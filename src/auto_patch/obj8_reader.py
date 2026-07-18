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
* ``OBJECT_AGL`` / ``OBJECT_MSL`` carry an explicit elevation in a
  column that plain ``OBJECT`` does not have, which SHIFTS THE HEADING
  ONE COLUMN RIGHT.  Verified against DSFTool text dumps on 2026-07-09
  (EGLL TaiModels, Nimbus KBNA); the whitespace-token layout is::

      OBJECT     <def_index> <lon> <lat>              <heading>
      OBJECT_AGL <def_index> <lon> <lat> <agl_offset> <heading>
      OBJECT_MSL <def_index> <lon> <lat> <msl_elev>   <heading>

  so heading is token 4 for ``OBJECT`` but token 5 for the other two
  (reading it from token 4 there silently swaps heading for elevation).
  The AGL offset is SIGNED (negative = below grade); the MSL value is an
  absolute elevation above sea level, never terrain-relative.  Only plain
  ``OBJECT`` is terrain-draped; ``OBJECT_AGL`` is terrain-relative at its
  anchor (A18) and ``OBJECT_MSL`` is absolute.
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
    """One object placement from a DSF text dump.

    Covers plain ``OBJECT`` rows, ``OBJECT_AGL`` rows (amendment A18) and
    — opt-in only — ``OBJECT_MSL`` rows.  ``placement_kind`` records which
    of the three the row was (``"OBJECT"`` / ``"OBJECT_AGL"`` /
    ``"OBJECT_MSL"``) so a caller need not re-derive it from which field
    is set.

    ``OBJECT`` / ``OBJECT_AGL`` are terrain-relative.  An AGL placement
    resolves to ``terrain(anchor) + above_ground_level_metres`` —
    terrain-relative AT THE ANCHOR ONLY, so far-flung geometry inherits
    the anchor's terrain exactly like a plain ``OBJECT`` does, offset by
    ``above_ground_level_metres`` (zero for plain ``OBJECT``).  HECA
    ships 183 of its 216 AGL placements on one shared anchor.

    ``above_ground_level_metres`` is the SIGNED above-ground offset (the
    field the object-terrain-features spec calls ``above_ground_offset_m``
    downstream — same value, no separate field): a NEGATIVE value is a
    below-grade signal, above-grade geometry authored below the terrain.
    EGLL places tunnels 6/7/10 as ``OBJECT_AGL`` at −1.0 / −7.0 / −7.5 m,
    invisible to a vertex-depth filter that ignores the placement offset.
    Never take ``abs()`` of this field: the sign is load-bearing.

    ``OBJECT_MSL`` rows carry an ABSOLUTE elevation in metres above sea
    level, not a terrain-relative offset; that value lands in
    ``mean_sea_level_elevation_m`` (``None`` for the terrain-relative
    kinds) and ``above_ground_level_metres`` stays ``0.0``.  These rows
    are skipped unless the reader is called with ``include_object_msl=
    True`` — at KBNA the twelve taxiway-bridge-deck fixtures at
    166.9994 m are the only absolute deck-elevation source in the pack,
    so downstream code opts in to read them; every existing caller that
    does not opt in sees exactly the pre-change behaviour (no MSL rows).
    """

    definition_index: int
    resource_path: str
    longitude: float
    latitude: float
    heading_degrees: float
    above_ground_level_metres: float = 0.0
    placement_kind: str = "OBJECT"
    mean_sea_level_elevation_m: float | None = None


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

    ``solid_triangle_hardness`` is parallel to ``solid_triangles``: entry
    ``i`` records the collision state in force when ``solid_triangles[i]``
    was emitted, one of ``""`` (not hard), ``"hard"`` (``ATTR_hard`` — a
    surface solid from every side) or ``"hard_deck"`` (``ATTR_hard_deck``
    — hard from ABOVE only; the deck carries collision while the space
    beneath stays passable, which is exactly why taxiway bridges and
    cut-and-cover tunnel decks use it: taxi on top, drive underneath).
    This is the object-terrain-features feature: it lets the tunnel/bridge
    classifier recover which triangles form a drivable deck without
    re-reading the file.  The field is a tuple so it can carry an
    immutable default of ``()`` (hand-constructed geometry in existing
    callers passes no hardness and reads back "unknown, treat as not
    hard"); ``load_object_file`` always populates it in full.

    OBJ8 hardness semantics (verified 2026-07-09 against the EGLL
    ``Airport/Tunnel/*.obj`` shells + decks, the KBNA
    ``KBNA_Bridge_Taxiway-L_p6.obj`` deck and the EDDF
    ``Bridge_*_hard.obj`` decks): ``ATTR_hard`` / ``ATTR_hard_deck`` set
    the state (each may carry a trailing surface-type token that is
    ignored here), ``ATTR_no_hard`` clears it, and the state persists
    across the following ``TRIS`` commands until changed — the same
    simple, non-stacked flag model this reader already uses for
    ``ATTR_draped`` (invariant I-9).  In the three exemplar packs every
    hard command seen is ``ATTR_hard_deck``: EGLL/EDDF set it once before
    all geometry (the whole object is a deck), while KBNA taxiway-L emits
    one plain ``TRIS`` (railing, not hard) and then ``ATTR_hard_deck``
    before the deck ``TRIS`` — so per-triangle tracking, not a
    whole-object flag, is required.
    """

    vertices: list[tuple[float, float, float]]
    solid_triangles: list[tuple[int, int, int]]
    draped_triangles: list[tuple[int, int, int]]
    positional_commands: list[PositionalCommand]
    animation_block_count: int
    level_of_detail_count: int
    vertex_line_indices: list[int]
    solid_triangle_hardness: tuple[str, ...] = ()
    # ``ATTR_layer_group_draped <group> <offset>`` — the draped draw
    # layer the object declares, or ``None`` when the file declares
    # none.  Ground-paint packs use it to stack base pavement UNDER
    # markings (HECA: base asphalt/concrete at ``("runways", 1)``,
    # taxi lines at ``("runways", 3)``, decals in group ``markings``),
    # which is exactly the signal the object-pavement classifier keys
    # on.  Only the LAST declaration in the file is kept (the exemplar
    # packs declare it once, in the header).
    draped_layer_group: tuple[str, int] | None = None

    def hard_deck_solid_triangles(self) -> list[tuple[int, int, int]]:
        """The subset of ``solid_triangles`` emitted under ``ATTR_hard_deck``
        — the drivable deck of a bridge or tunnel.  Defensive against a
        hardness tuple shorter than ``solid_triangles`` (hand-constructed
        geometry with the default empty tuple): a missing entry reads as
        not hard."""
        return [
            triangle
            for index, triangle in enumerate(self.solid_triangles)
            if index < len(self.solid_triangle_hardness)
            and self.solid_triangle_hardness[index] == "hard_deck"
        ]

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
    I-17); tracks ``ATTR_draped`` / ``ATTR_no_draped`` state and — for the
    object-terrain-features feature — ``ATTR_hard`` / ``ATTR_hard_deck`` /
    ``ATTR_no_hard`` state across the ``TRIS`` commands (see
    :class:`ObjectGeometry` for the verified hardness semantics); collects
    :class:`PositionalCommand` entries and counts ``ANIM_begin`` /
    ``ATTR_LOD``.
    """
    vertices: list[tuple[float, float, float]] = []
    vertex_line_indices: list[int] = []
    indices: list[int] = []
    triangle_ranges: list[tuple[int, int, bool, str]] = []
    positional_commands: list[PositionalCommand] = []
    animation_block_count = 0
    level_of_detail_count = 0
    currently_draped = False
    currently_hard = ""  # "" | "hard" | "hard_deck"
    draped_layer_group: tuple[str, int] | None = None

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
            elif keyword == "ATTR_hard_deck":
                currently_hard = "hard_deck"
            elif keyword == "ATTR_hard":
                currently_hard = "hard"
            elif keyword == "ATTR_no_hard":
                currently_hard = ""
            elif keyword == "ATTR_layer_group_draped":
                # ``ATTR_layer_group_draped <group> [<offset>]`` — a
                # missing or non-numeric offset reads as 0 (the X-Plane
                # default).  Malformed lines leave the field untouched.
                if len(tokens) >= 2:
                    try:
                        layer_offset = (
                            int(tokens[2]) if len(tokens) >= 3 else 0)
                    except ValueError:
                        layer_offset = 0
                    draped_layer_group = (tokens[1].lower(), layer_offset)
            elif keyword == "TRIS":
                triangle_ranges.append(
                    (
                        int(tokens[1]),
                        int(tokens[2]),
                        currently_draped,
                        currently_hard,
                    )
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
                # Parallel to ``solid``: the hardness in force for this
                # triangle.  Draped triangles never carry hardness.
                solid_hardness.append(hardness)
    return ObjectGeometry(
        vertices=vertices,
        solid_triangles=solid,
        draped_triangles=draped,
        positional_commands=positional_commands,
        animation_block_count=animation_block_count,
        level_of_detail_count=level_of_detail_count,
        vertex_line_indices=vertex_line_indices,
        solid_triangle_hardness=tuple(solid_hardness),
        draped_layer_group=draped_layer_group,
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
    include_object_msl: bool = False,
) -> list[ObjectPlacement]:
    """Collect ``OBJECT`` placements from a DSF text dump.

    ``OBJECT`` rows and — amendment A18 — ``OBJECT_AGL`` rows are always
    collected: an AGL placement is terrain-relative at its ANCHOR only,
    so it carries the distant-anchor disease with a constant vertical
    offset (``above_ground_level_metres``, signed; the heading moves to
    the fifth column — see the module docstring's verified column table).

    ``OBJECT_MSL`` rows carry an ABSOLUTE elevation above sea level and
    are skipped by default (``include_object_msl=False``), preserving the
    historical behaviour exactly: existing callers that do not opt in
    receive no MSL rows.  Pass ``include_object_msl=True`` to receive them
    with ``placement_kind == "OBJECT_MSL"`` and their absolute elevation
    in ``mean_sea_level_elevation_m`` — at KBNA these are the only
    absolute deck-elevation source (twelve taxiway-bridge fixtures at
    166.9994 m), so bridge classification opts in.

    Takes lines, not a path, so tests feed synthetic text (harness
    pattern (a), ``tests/test_agp_reader.py``).
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
