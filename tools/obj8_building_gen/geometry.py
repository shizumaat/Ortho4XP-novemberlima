"""Parametric building-geometry vocabulary for X-Plane OBJ8 authoring.

Coordinate conventions (X-Plane object space):
  +X east, +Y up, +Z south; units meters; origin at the placement point.
  OBJ8 triangles are CLOCKWISE-front: the visible side of a face is the
  one from which its vertices appear in clockwise order (verified
  empirically against X-Plane 12 stock objects, 49k+ triangles).

"Map view" in this module means looking down with north up and east
right, i.e. plotting (x, -z). Footprint rings may be supplied in either
orientation; they are normalized internally to counter-clockwise in map
view, whose outward edge normal for an edge A→B is (-dz, 0, dx).

All primitives mutate a Mesh in place. Faces are emitted with the
desired outward normal stated explicitly; winding is derived from it,
so callers never reason about vertex order.

Build-time impact: none — not part of the tile build pipeline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .atlas import AtlasBand, AtlasRect

Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]

_DEGENERATE_AREA = 1e-9


def _subtract(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _normalize(vector: Vec3) -> Vec3:
    length = math.sqrt(_dot(vector, vector))
    if length < 1e-12:
        raise ValueError(f"cannot normalize near-zero vector {vector}")
    return (vector[0] / length, vector[1] / length, vector[2] / length)


class Mesh:
    """Triangle soup with per-vertex position, normal, and UV."""

    def __init__(self) -> None:
        self.vertices: list[tuple[float, float, float, float, float, float, float, float]] = []
        self.indices: list[int] = []

    @property
    def triangle_count(self) -> int:
        return len(self.indices) // 3

    def _emit_vertex(self, position: Vec3, normal: Vec3, uv: Vec2) -> int:
        self.vertices.append((*position, *normal, *uv))
        return len(self.vertices) - 1

    def add_face(
        self,
        corners: Sequence[Vec3],
        uvs: Sequence[Vec2],
        outward: Vec3,
    ) -> None:
        """Add a planar convex face visible from the ``outward`` side.

        The face is fan-triangulated from the first corner. Each
        triangle's winding is chosen so its clockwise-front side faces
        ``outward`` (dot of the right-handed geometric normal with
        ``outward`` is negative). Degenerate slivers are skipped.
        """
        if len(corners) != len(uvs) or len(corners) < 3:
            raise ValueError("corners and uvs must match and have length >= 3")
        outward = _normalize(outward)
        base_indices = [
            self._emit_vertex(position, outward, uv)
            for position, uv in zip(corners, uvs)
        ]
        for k in range(1, len(corners) - 1):
            geometric_normal = _cross(
                _subtract(corners[k], corners[0]),
                _subtract(corners[k + 1], corners[0]),
            )
            magnitude = math.sqrt(_dot(geometric_normal, geometric_normal))
            if magnitude < _DEGENERATE_AREA:
                continue
            if _dot(geometric_normal, outward) > 0.0:
                triangle = (base_indices[0], base_indices[k + 1], base_indices[k])
            else:
                triangle = (base_indices[0], base_indices[k], base_indices[k + 1])
            self.indices.extend(triangle)

    def merge(self, other: "Mesh") -> None:
        offset = len(self.vertices)
        self.vertices.extend(other.vertices)
        self.indices.extend(index + offset for index in other.indices)

    def bounds(self) -> tuple[Vec3, Vec3]:
        if not self.vertices:
            raise ValueError("empty mesh has no bounds")
        xs = [v[0] for v in self.vertices]
        ys = [v[1] for v in self.vertices]
        zs = [v[2] for v in self.vertices]
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


@dataclass(frozen=True)
class Frame:
    """A local placement frame on the ground plane.

    ``along`` runs at ``bearing_degrees`` true (0 = north, 90 = east);
    ``across`` runs 90 degrees to the right of ``along`` in map view.
    """

    origin_x: float = 0.0
    origin_z: float = 0.0
    bearing_degrees: float = 0.0

    def to_world(self, along: float, across: float) -> Vec2:
        bearing = math.radians(self.bearing_degrees)
        along_x, along_z = math.sin(bearing), -math.cos(bearing)
        across_x, across_z = math.cos(bearing), math.sin(bearing)
        return (
            self.origin_x + along * along_x + across * across_x,
            self.origin_z + along * along_z + across * across_z,
        )

    def point(self, along: float, across: float, y: float) -> Vec3:
        x, z = self.to_world(along, across)
        return (x, y, z)


# ---------------------------------------------------------------------------
# Footprint utilities
# ---------------------------------------------------------------------------


def _signed_area_xz(ring: Sequence[Vec2]) -> float:
    total = 0.0
    for k in range(len(ring)):
        x0, z0 = ring[k]
        x1, z1 = ring[(k + 1) % len(ring)]
        total += x0 * z1 - x1 * z0
    return 0.5 * total


def normalize_footprint(ring: Sequence[Vec2]) -> list[Vec2]:
    """Return the ring open (no repeated last point), counter-clockwise
    in map view. CCW-in-map corresponds to NEGATIVE shoelace area in raw
    (x, z) coordinates because z points south."""
    points = list(ring)
    if len(points) >= 2 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 3:
        raise ValueError("footprint needs at least 3 distinct points")
    if _signed_area_xz(points) > 0.0:
        points.reverse()
    return points


def footprint_area(ring: Sequence[Vec2]) -> float:
    """Absolute enclosed area of a ring in square meters."""
    return abs(_signed_area_xz(normalize_footprint(ring)))


def _point_in_triangle(point: Vec2, a: Vec2, b: Vec2, c: Vec2) -> bool:
    def edge_sign(p: Vec2, q: Vec2, r: Vec2) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d0, d1, d2 = edge_sign(a, b, point), edge_sign(b, c, point), edge_sign(c, a, point)
    epsilon = 1e-12
    return d0 > epsilon and d1 > epsilon and d2 > epsilon


def triangulate_footprint(ring: Sequence[Vec2]) -> tuple[list[Vec2], list[tuple[int, int, int]]]:
    """Ear-clip a simple polygon. Returns (normalized ring, index triples).

    Works in map coordinates on the CCW-normalized ring. Falls back to a
    fan from vertex 0 if no ear is found (degenerate input); results are
    winding-agnostic because faces built from them state their outward
    normal explicitly.
    """
    points = normalize_footprint(ring)
    map_points = [(x, -z) for x, z in points]
    remaining = list(range(len(map_points)))
    triangles: list[tuple[int, int, int]] = []
    stall_guard = 0
    while len(remaining) > 3 and stall_guard < 10 * len(map_points):
        stall_guard += 1
        clipped_one = False
        for position in range(len(remaining)):
            index_previous = remaining[position - 1]
            index_current = remaining[position]
            index_next = remaining[(position + 1) % len(remaining)]
            a, b, c = (
                map_points[index_previous],
                map_points[index_current],
                map_points[index_next],
            )
            convexity = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            if convexity <= 1e-12:
                continue
            others = (
                map_points[i]
                for i in remaining
                if i not in (index_previous, index_current, index_next)
            )
            if any(_point_in_triangle(p, a, b, c) for p in others):
                continue
            triangles.append((index_previous, index_current, index_next))
            remaining.pop(position)
            clipped_one = True
            break
        if not clipped_one:
            break
    if len(remaining) == 3:
        triangles.append((remaining[0], remaining[1], remaining[2]))
    elif len(remaining) > 3:
        anchor = remaining[0]
        for position in range(1, len(remaining) - 1):
            triangles.append((anchor, remaining[position], remaining[position + 1]))
    return points, triangles


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def wall_run(
    mesh: Mesh,
    points: Sequence[Vec2],
    bottom_y: float,
    top_y: float,
    band: AtlasBand,
    close: bool = False,
    v_zero_y: float | None = None,
    flip_outward: bool = False,
) -> None:
    """Emit vertical wall quads along a polyline of (x, z) points.

    For a polyline that follows a CCW-in-map footprint, the outward face
    is to the right of travel: (-dz, 0, dx). ``flip_outward`` reverses
    that (interior walls). U accumulates real distance along the run so
    the band pattern is continuous across corners; V maps height above
    ``v_zero_y`` (defaults to ``bottom_y``) through the band.
    """
    if v_zero_y is None:
        v_zero_y = bottom_y
    path = list(points)
    if close:
        path.append(path[0])
    distance = 0.0
    for (x0, z0), (x1, z1) in zip(path[:-1], path[1:]):
        segment_length = math.hypot(x1 - x0, z1 - z0)
        if segment_length < 1e-9:
            continue
        outward: Vec3 = (-(z1 - z0) / segment_length, 0.0, (x1 - x0) / segment_length)
        if flip_outward:
            outward = (-outward[0], 0.0, -outward[2])
        u_start = band.u_for(distance)
        u_end = band.u_for(distance + segment_length)
        v_low = band.v_for(bottom_y - v_zero_y)
        v_high = band.v_for(top_y - v_zero_y)
        mesh.add_face(
            corners=(
                (x0, bottom_y, z0),
                (x1, bottom_y, z1),
                (x1, top_y, z1),
                (x0, top_y, z0),
            ),
            uvs=((u_start, v_low), (u_end, v_low), (u_end, v_high), (u_start, v_high)),
            outward=outward,
        )
        distance += segment_length


def extrude_footprint(
    mesh: Mesh,
    footprint: Sequence[Vec2],
    bottom_y: float,
    top_y: float,
    band: AtlasBand,
    v_zero_y: float | None = None,
) -> list[Vec2]:
    """Extrude a footprint ring into exterior walls; returns the
    normalized (CCW-in-map, open) ring for reuse by caps."""
    ring = normalize_footprint(footprint)
    wall_run(mesh, ring, bottom_y, top_y, band, close=True, v_zero_y=v_zero_y)
    return ring


def polygon_cap(
    mesh: Mesh,
    footprint: Sequence[Vec2],
    y: float,
    rect: AtlasRect,
    facing_up: bool = True,
) -> None:
    """Horizontal cap over a simple polygon, UV-stretched to ``rect``."""
    ring, triangles = triangulate_footprint(footprint)
    xs = [p[0] for p in ring]
    zs = [p[1] for p in ring]
    x_min, x_max = min(xs), max(xs)
    z_min, z_max = min(zs), max(zs)
    x_span = max(x_max - x_min, 1e-9)
    z_span = max(z_max - z_min, 1e-9)
    outward: Vec3 = (0.0, 1.0, 0.0) if facing_up else (0.0, -1.0, 0.0)

    def uv_of(point: Vec2) -> Vec2:
        return rect.uv_for(
            (point[0] - x_min) / x_span,
            (z_max - point[1]) / z_span,  # north (low z) maps to high V
        )

    for ia, ib, ic in triangles:
        corners = tuple((ring[i][0], y, ring[i][1]) for i in (ia, ib, ic))
        mesh.add_face(corners, tuple(uv_of(ring[i]) for i in (ia, ib, ic)), outward)


def box(
    mesh: Mesh,
    frame: Frame,
    along_range: tuple[float, float],
    across_range: tuple[float, float],
    bottom_y: float,
    top_y: float,
    wall_band: AtlasBand,
    cap_rect: AtlasRect | None = None,
    v_zero_y: float | None = None,
) -> None:
    """Rectangular block in a frame: four walls and an optional flat cap."""
    a0, a1 = along_range
    c0, c1 = across_range
    corners = [
        frame.to_world(a0, c0),
        frame.to_world(a1, c0),
        frame.to_world(a1, c1),
        frame.to_world(a0, c1),
    ]
    ring = extrude_footprint(mesh, corners, bottom_y, top_y, wall_band, v_zero_y=v_zero_y)
    if cap_rect is not None:
        polygon_cap(mesh, ring, top_y, cap_rect)


def gable_roof(
    mesh: Mesh,
    frame: Frame,
    along_range: tuple[float, float],
    across_range: tuple[float, float],
    eave_y: float,
    ridge_y: float,
    overhang: float,
    roof_band: AtlasBand,
    fascia_rect: AtlasRect,
    fascia_depth: float = 0.35,
    end_wall_rect: AtlasRect | None = None,
    soffit_rect: AtlasRect | None = None,
    ridge_across_fraction: float = 0.5,
) -> None:
    """Gable roof with the ridge parallel to ``along``.

    ``ridge_across_fraction`` places the ridge within ``across_range``
    (0.5 = symmetric; smaller values pull it toward ``across_range[0]``,
    giving an asymmetric gable with a short steep slope on that side).
    Emits two slope planes (band-mapped: U repeats along the ridge, V
    stretched eave→ridge), their soffit undersides, fascia strips on the
    eave and raked edges, and optional triangular gable-end infill walls
    at the ends of ``along_range`` (at the building line, not the
    overhang line).
    """
    a0, a1 = along_range
    c0, c1 = across_range
    c_mid = c0 + ridge_across_fraction * (c1 - c0)
    a_low, a_high = a0 - overhang, a1 + overhang
    c_low, c_high = c0 - overhang, c1 + overhang
    u_low = roof_band.u_for(0.0)
    u_high = roof_band.u_for(a_high - a_low)
    v_eave = roof_band.v_for(0.0)
    v_ridge = roof_band.v_for(roof_band.height_meters)

    def slope(c_eave: float) -> None:
        ridge_a_low = frame.point(a_low, c_mid, ridge_y)
        ridge_a_high = frame.point(a_high, c_mid, ridge_y)
        eave_a_high = frame.point(a_high, c_eave, eave_y)
        eave_a_low = frame.point(a_low, c_eave, eave_y)
        plane_normal = _cross(
            _subtract(ridge_a_high, ridge_a_low),
            _subtract(eave_a_low, ridge_a_low),
        )
        if plane_normal[1] < 0.0:
            plane_normal = (-plane_normal[0], -plane_normal[1], -plane_normal[2])
        top_corners = (eave_a_low, eave_a_high, ridge_a_high, ridge_a_low)
        top_uvs = ((u_low, v_eave), (u_high, v_eave), (u_high, v_ridge), (u_low, v_ridge))
        mesh.add_face(top_corners, top_uvs, plane_normal)
        # Soffit: same plane dropped by the fascia depth, facing down.
        drop = (0.0, -fascia_depth, 0.0)
        bottom_corners = tuple(
            (p[0] + drop[0], p[1] + drop[1], p[2] + drop[2]) for p in top_corners
        )
        if soffit_rect is not None:
            soffit_uvs = (
                soffit_rect.uv_for(0.0, 0.0),
                soffit_rect.uv_for(1.0, 0.0),
                soffit_rect.uv_for(1.0, 1.0),
                soffit_rect.uv_for(0.0, 1.0),
            )
        else:
            soffit_uvs = top_uvs
        mesh.add_face(
            bottom_corners,
            soffit_uvs,
            (-plane_normal[0], -plane_normal[1], -plane_normal[2]),
        )
        # Fascia: eave edge plus the two raked (sloped) end edges.
        for edge_start, edge_end in (
            (eave_a_low, eave_a_high),
            (ridge_a_low, eave_a_low),
            (eave_a_high, ridge_a_high),
        ):
            lower_start = (edge_start[0], edge_start[1] - fascia_depth, edge_start[2])
            lower_end = (edge_end[0], edge_end[1] - fascia_depth, edge_end[2])
            edge_vector = _subtract(edge_end, edge_start)
            horizontal = _cross(edge_vector, (0.0, 1.0, 0.0))
            if all(abs(component) < 1e-9 for component in horizontal):
                continue
            center_a = 0.5 * (a_low + a_high)
            center = frame.point(center_a, c_mid, eave_y)
            midpoint = (
                0.5 * (edge_start[0] + edge_end[0]),
                0.0,
                0.5 * (edge_start[2] + edge_end[2]),
            )
            to_edge = (midpoint[0] - center[0], 0.0, midpoint[2] - center[2])
            if _dot(horizontal, to_edge) < 0.0:
                horizontal = (-horizontal[0], -horizontal[1], -horizontal[2])
            mesh.add_face(
                (edge_start, edge_end, lower_end, lower_start),
                (
                    fascia_rect.uv_for(0.0, 1.0),
                    fascia_rect.uv_for(1.0, 1.0),
                    fascia_rect.uv_for(1.0, 0.0),
                    fascia_rect.uv_for(0.0, 0.0),
                ),
                horizontal,
            )

    slope(c_high)
    slope(c_low)

    if end_wall_rect is not None:
        for a_end, outward_along in ((a0, -1.0), (a1, 1.0)):
            eave_near = frame.point(a_end, c0, eave_y)
            eave_far = frame.point(a_end, c1, eave_y)
            apex = frame.point(a_end, c_mid, ridge_y)
            world_direction = frame.to_world(outward_along, 0.0)
            origin = frame.to_world(0.0, 0.0)
            outward = (
                world_direction[0] - origin[0],
                0.0,
                world_direction[1] - origin[1],
            )
            mesh.add_face(
                (eave_near, eave_far, apex),
                (
                    end_wall_rect.uv_for(0.0, 0.0),
                    end_wall_rect.uv_for(1.0, 0.0),
                    end_wall_rect.uv_for(0.5, 1.0),
                ),
                outward,
            )


def shed_roof(
    mesh: Mesh,
    frame: Frame,
    along_range: tuple[float, float],
    across_range: tuple[float, float],
    high_y: float,
    low_y: float,
    overhang: float,
    roof_band: AtlasBand,
    fascia_rect: AtlasRect,
    fascia_depth: float = 0.3,
    high_side_low_across: bool = True,
    soffit_rect: AtlasRect | None = None,
) -> None:
    """Single-slope roof over a rectangle; high edge on the low-``across``
    side by default."""
    a0, a1 = along_range
    c0, c1 = across_range
    a_low, a_high = a0 - overhang, a1 + overhang
    c_low, c_high = c0 - overhang, c1 + overhang
    if high_side_low_across:
        c_at_high, c_at_low = c_low, c_high
    else:
        c_at_high, c_at_low = c_high, c_low
    high_a_low = frame.point(a_low, c_at_high, high_y)
    high_a_high = frame.point(a_high, c_at_high, high_y)
    low_a_high = frame.point(a_high, c_at_low, low_y)
    low_a_low = frame.point(a_low, c_at_low, low_y)
    plane_normal = _cross(
        _subtract(high_a_high, high_a_low), _subtract(low_a_low, high_a_low)
    )
    if plane_normal[1] < 0.0:
        plane_normal = (-plane_normal[0], -plane_normal[1], -plane_normal[2])
    corners = (low_a_low, low_a_high, high_a_high, high_a_low)
    u_low, u_high = roof_band.u_for(0.0), roof_band.u_for(a_high - a_low)
    uvs = (
        (u_low, roof_band.v_for(0.0)),
        (u_high, roof_band.v_for(0.0)),
        (u_high, roof_band.v_for(roof_band.height_meters)),
        (u_low, roof_band.v_for(roof_band.height_meters)),
    )
    mesh.add_face(corners, uvs, plane_normal)
    bottom_corners = tuple((p[0], p[1] - fascia_depth, p[2]) for p in corners)
    if soffit_rect is not None:
        soffit_uvs = (
            soffit_rect.uv_for(0.0, 0.0),
            soffit_rect.uv_for(1.0, 0.0),
            soffit_rect.uv_for(1.0, 1.0),
            soffit_rect.uv_for(0.0, 1.0),
        )
    else:
        soffit_uvs = uvs
    mesh.add_face(
        bottom_corners, soffit_uvs, (-plane_normal[0], -plane_normal[1], -plane_normal[2])
    )
    edges = (
        (low_a_low, low_a_high),
        (high_a_high, high_a_low),
        (high_a_low, low_a_low),
        (low_a_high, high_a_high),
    )
    center = frame.point(0.5 * (a_low + a_high), 0.5 * (c_low + c_high), low_y)
    for edge_start, edge_end in edges:
        lower_start = (edge_start[0], edge_start[1] - fascia_depth, edge_start[2])
        lower_end = (edge_end[0], edge_end[1] - fascia_depth, edge_end[2])
        horizontal = _cross(_subtract(edge_end, edge_start), (0.0, 1.0, 0.0))
        if all(abs(component) < 1e-9 for component in horizontal):
            continue
        midpoint = (
            0.5 * (edge_start[0] + edge_end[0]),
            0.0,
            0.5 * (edge_start[2] + edge_end[2]),
        )
        to_edge = (midpoint[0] - center[0], 0.0, midpoint[2] - center[2])
        if _dot(horizontal, to_edge) < 0.0:
            horizontal = (-horizontal[0], -horizontal[1], -horizontal[2])
        mesh.add_face(
            (edge_start, edge_end, lower_end, lower_start),
            (
                fascia_rect.uv_for(0.0, 1.0),
                fascia_rect.uv_for(1.0, 1.0),
                fascia_rect.uv_for(1.0, 0.0),
                fascia_rect.uv_for(0.0, 0.0),
            ),
            horizontal,
        )


def canopy(
    mesh: Mesh,
    frame: Frame,
    along_range: tuple[float, float],
    across_range: tuple[float, float],
    deck_y: float,
    thickness: float,
    fascia_band: AtlasBand,
    deck_rect: AtlasRect,
    column_positions: Sequence[Vec2] = (),
    column_size: float = 0.4,
    column_band: AtlasBand | None = None,
) -> None:
    """Flat canopy slab with optional square columns to the ground.

    ``column_positions`` are (along, across) pairs in the frame.
    """
    a0, a1 = along_range
    c0, c1 = across_range
    corners = [
        frame.to_world(a0, c0),
        frame.to_world(a1, c0),
        frame.to_world(a1, c1),
        frame.to_world(a0, c1),
    ]
    ring = extrude_footprint(
        mesh, corners, deck_y - thickness, deck_y, fascia_band
    )
    polygon_cap(mesh, ring, deck_y, deck_rect, facing_up=True)
    polygon_cap(mesh, ring, deck_y - thickness, deck_rect, facing_up=False)
    band = column_band if column_band is not None else fascia_band
    half = 0.5 * column_size
    for along, across in column_positions:
        box(
            mesh,
            frame,
            (along - half, along + half),
            (across - half, across + half),
            0.0,
            deck_y - thickness,
            band,
        )


def prism(
    mesh: Mesh,
    frame: Frame,
    center_along: float,
    center_across: float,
    radius: float,
    bottom_y: float,
    top_y: float,
    band: AtlasBand,
    sides: int = 8,
    cap_rect: AtlasRect | None = None,
) -> None:
    """Regular n-gon column (e.g. a jet-bridge rotunda or round pier)."""
    ring = []
    for k in range(sides):
        angle = 2.0 * math.pi * (k + 0.5) / sides
        ring.append(
            frame.to_world(
                center_along + radius * math.cos(angle),
                center_across + radius * math.sin(angle),
            )
        )
    normalized = extrude_footprint(mesh, ring, bottom_y, top_y, band)
    if cap_rect is not None:
        polygon_cap(mesh, normalized, top_y, cap_rect)


def oriented_slab(
    mesh: Mesh,
    start_xz: Vec2,
    start_top_y: float,
    end_xz: Vec2,
    end_top_y: float,
    width: float,
    thickness: float,
    side_band: AtlasBand,
    top_rect: AtlasRect,
) -> None:
    """A rectangular-section tube from one point to another, horizontal
    cross-section axis (no roll) — jet-bridge telescopes, sloped links.

    The centerline runs along the TOP face; the slab extends
    ``thickness`` downward. Sides map through ``side_band`` (U = length
    along the tube), top/bottom/ends stretch ``top_rect``.
    """
    (x0, z0), (x1, z1) = start_xz, end_xz
    length = math.hypot(x1 - x0, z1 - z0)
    if length < 1e-6:
        raise ValueError("oriented_slab needs distinct endpoints")
    across = (-(z1 - z0) / length, (x1 - x0) / length)
    half = 0.5 * width
    corners_top = [
        (x0 - across[0] * half, start_top_y, z0 - across[1] * half),
        (x0 + across[0] * half, start_top_y, z0 + across[1] * half),
        (x1 + across[0] * half, end_top_y, z1 + across[1] * half),
        (x1 - across[0] * half, end_top_y, z1 - across[1] * half),
    ]
    corners_bottom = [(x, y - thickness, z) for x, y, z in corners_top]
    u0, u1 = side_band.u_for(0.0), side_band.u_for(length)
    v_low = side_band.v_for(0.0)
    v_high = side_band.v_for(side_band.height_meters)
    side_uvs = ((u0, v_low), (u1, v_low), (u1, v_high), (u0, v_high))
    # Side walls (outward = -across and +across).
    mesh.add_face(
        (corners_bottom[0], corners_bottom[3], corners_top[3], corners_top[0]),
        side_uvs, (-across[0], 0.0, -across[1]),
    )
    mesh.add_face(
        (corners_bottom[1], corners_bottom[2], corners_top[2], corners_top[1]),
        side_uvs, (across[0], 0.0, across[1]),
    )
    rect_uvs = (
        top_rect.uv_for(0.0, 0.0), top_rect.uv_for(1.0, 0.0),
        top_rect.uv_for(1.0, 1.0), top_rect.uv_for(0.0, 1.0),
    )
    rise = (end_top_y - start_top_y) / length
    axis = ((x1 - x0) / length, rise, (z1 - z0) / length)
    top_normal = _normalize((-axis[1] * axis[0], 1.0, -axis[1] * axis[2]))
    mesh.add_face(tuple(corners_top), rect_uvs, top_normal)
    mesh.add_face(tuple(corners_bottom), rect_uvs, (-top_normal[0], -top_normal[1], -top_normal[2]))
    # End caps.
    mesh.add_face(
        (corners_top[0], corners_top[1], corners_bottom[1], corners_bottom[0]),
        rect_uvs, (-axis[0], -axis[1], -axis[2]),
    )
    mesh.add_face(
        (corners_top[3], corners_top[2], corners_bottom[2], corners_bottom[3]),
        rect_uvs, axis,
    )
