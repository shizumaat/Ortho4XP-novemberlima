"""Consult a correlating author BASE MESH to confirm object-derived cutouts.

Workstream W-V of ``docs/object_terrain_features_spec.md``, implementing
ruling R11 and amendment A6: when a custom base mesh exists and correlates
with a custom airport pack (the EGLL_MESH case), the author's own
heightfield is the buried-versus-exposed ORACLE.  This tool parses that
base-mesh DSF, samples it under every classified structure (tunnels,
bridges, and any below-grade building geometry), and reports for each:

    local datum     the modal elevation of a margin ring around the
                    footprint (the author's flattened platform there —
                    21 m over the EGLL field, 28 m over the NW rising
                    ground).
    verdict         EXPOSED / PARTIAL / BURIED, by the A6 threshold: a dip
                    of at least :data:`DIP_BELOW_DATUM_M` (1 m) over at
                    least :data:`EXPOSED_FOOTPRINT_FRACTION` (25%) of the
                    footprint is EXPOSED (the author cut a trench there);
                    a flat-at-datum footprint is BURIED (the author left
                    the mesh flat and the object carries its own walls).
    floor / depth   minimum interior elevation and ``datum - floor``.
    cutout stats    where EXPOSED, the area/extent of the sub-datum cells.

It resolves the two cases R11 names: the EGLL ``4.obj`` bridge-versus-buried
call, and (with ``--depressions``) the KDEN Jeppesen escalator case — an
author interior depression with almost no modeled hall above it.

The parser, and the one gotcha it encodes
------------------------------------------
A MeshTool DSF stores terrain as physical patches (the ``BEGIN_PATCH``
flag's bit 0 is the physical bit), each a pool of ``PATCH_VERTEX`` rows
(``longitude latitude elevation ...``) grouped into primitives.  Primitive
type 1 is a triangle STRIP and MUST be decoded as a strip (alternating
winding); decoding a strip as a fan — or a fan as a strip — fabricates
phantom triangles that read as holes/spikes.  This tool decodes type 0 as
triangle lists, type 1 as strips, type 2 as fans (verified against the
EGLL author mesh: 100% consistent-winding as strips, near-0% as fans).
Vertices whose elevation column is the raster-reference sentinel
(``<= -32000``) carry no baked elevation (they drape on the base raster)
and are dropped from the sampled set.  The logic is promoted, self-
contained, from the 2026-07-09 session probe ``parse_mesh.py`` — no
scratchpad dependency at runtime.

Usage:
    venv/bin/python tools/custom_mesh_cutout_oracle.py \
        <author_base_mesh.dsf | author_dump.txt> \
        <overlay_tile.dsf | overlay_pack_root> \
        [--xplane-root DIR] [--depressions]
        [--grid 40] [--margin-metres 20]

Example (the EGLL author mesh versus the TaiModels overlay):
    venv/bin/python tools/custom_mesh_cutout_oracle.py \
        "/Volumes/.../c_GBR - 100_airport - EGLL_MESH/Earth nav data/+50-010/+51-001.dsf" \
        "$XP/Custom Scenery/c_GBR - 100_airport - EGLL_LONDON_TAIMODELS/Earth nav data/+50-010/+51-001.dsf"
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict

import numpy

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(os.path.dirname(_TOOLS_DIR), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from auto_patch import dsf_reader, obj8_reader, object_terrain_features
from auto_patch.object_terrain_features import (
    frame_polygon_to_longitude_latitude,
)

# ---------------------------------------------------------------------------
# A6 verdict thresholds (spec sections 2.4 / 3.4, amendment A6) — named
# constants, no magic numbers at the call sites.
# ---------------------------------------------------------------------------

# A mesh sample this far below the local datum counts as "dipped".
DIP_BELOW_DATUM_M = 1.0

# EXPOSED when at least this fraction of interior samples dip; PARTIAL down
# to :data:`PARTIAL_FOOTPRINT_FRACTION`; below that, BURIED.
EXPOSED_FOOTPRINT_FRACTION = 0.25
PARTIAL_FOOTPRINT_FRACTION = 0.05

# Elevation column values at or below this are the raster-reference
# sentinel (no baked elevation) and are dropped.
RASTER_REFERENCE_SENTINEL = -32000.0

# A below-grade building candidate reaches at least this far below grade
# (effective height) — the A6 non-tunnel below-grade set (basements,
# jetway slack) whose burial the oracle confirms.
BUILDING_BELOW_GRADE_M = 1.0

# --depressions: an interior depression at least this deep and this large
# is reported (the A6 reverse pass; the KDEN interior-hall analogue).
DEPRESSION_MIN_DEPTH_M = 2.0
DEPRESSION_MIN_AREA_M2 = 400.0

# Ring at least this far (metres) outside a footprint gives the local datum
# and the enclosure test.
DATUM_RING_MARGIN_M = 20.0

MAXIMUM_PLACEMENTS_PER_RESOURCE = 50


# ---------------------------------------------------------------------------
# Author base-mesh parsing (promoted from parse_mesh.py, self-contained)
# ---------------------------------------------------------------------------

def _dump_mesh_text(dsf_or_text_path: str) -> str:
    """Return a path to the DSFTool text dump of a base-mesh DSF.

    A ``.txt`` argument is taken as an already-dumped mesh and returned
    unchanged (so a pre-dumped file can be reused); a ``.dsf`` is converted
    with DSFTool into a temp file keyed by ``(abspath, mtime)`` so repeated
    runs do not re-dump the 200-plus-MB text."""
    if dsf_or_text_path.lower().endswith(".txt"):
        return dsf_or_text_path
    tool = dsf_reader._dsftool_path()
    if tool is None:
        raise SystemExit("DSFTool binary not found; cannot dump the base mesh")
    try:
        mtime = int(os.path.getmtime(dsf_or_text_path))
    except OSError as error:
        raise SystemExit(f"cannot stat {dsf_or_text_path}: {error}")
    cache_name = (
        f"o4_mesh_dump_{abs(hash(os.path.abspath(dsf_or_text_path)))}_{mtime}.txt"
    )
    cache_path = os.path.join(tempfile.gettempdir(), cache_name)
    if not os.path.isfile(cache_path):
        subprocess.run(
            [tool, "--dsf2text", dsf_or_text_path, cache_path],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    return cache_path


def parse_author_mesh_triangles(
    text_path: str,
    bounds: tuple[float, float, float, float] | None = None,
) -> numpy.ndarray:
    """Stream-parse the physical-mesh triangles of a base-mesh text dump.

    Returns an ``(N, 9)`` float64 array of triangle corner coordinates
    ``(lon0, lat0, elev0, lon1, lat1, elev1, lon2, lat2, elev2)``.  Only
    triangles with a corner inside ``bounds`` (``(min_lon, min_lat,
    max_lon, max_lat)``, whole tile when ``None``) are kept.  Type-1
    primitives are decoded as STRIPS, type-2 as FANS, type-0 as LISTS
    (module docstring's gotcha)."""
    minimum_longitude, minimum_latitude, maximum_longitude, maximum_latitude = (
        bounds if bounds is not None else (-1e9, -1e9, 1e9, 1e9)
    )

    def _in_bounds(longitude, latitude) -> bool:
        return (minimum_longitude <= longitude <= maximum_longitude
                and minimum_latitude <= latitude <= maximum_latitude)

    triangles: list[tuple] = []
    in_physical = False
    primitive_type: int | None = None
    pool: list[tuple[float, float, float]] = []

    def _emit(corner_a, corner_b, corner_c) -> None:
        if (_in_bounds(corner_a[0], corner_a[1])
                or _in_bounds(corner_b[0], corner_b[1])
                or _in_bounds(corner_c[0], corner_c[1])):
            triangles.append((
                corner_a[0], corner_a[1], corner_a[2],
                corner_b[0], corner_b[1], corner_b[2],
                corner_c[0], corner_c[1], corner_c[2],
            ))

    def _flush() -> None:
        nonlocal pool
        count = len(pool)
        if primitive_type == 0:  # triangle list
            for base in range(0, count - count % 3, 3):
                _emit(pool[base], pool[base + 1], pool[base + 2])
        elif primitive_type == 1:  # triangle STRIP (winding alternates)
            for base in range(count - 2):
                if base % 2 == 0:
                    _emit(pool[base], pool[base + 1], pool[base + 2])
                else:
                    _emit(pool[base + 1], pool[base], pool[base + 2])
        elif primitive_type == 2:  # triangle FAN
            for base in range(1, count - 1):
                _emit(pool[0], pool[base], pool[base + 1])
        pool = []

    with open(text_path, errors="replace") as handle:
        for line in handle:
            if not line or line[0] == "#":
                continue
            tokens = line.split()
            if not tokens:
                continue
            command = tokens[0]
            if command == "BEGIN_PATCH":
                # BEGIN_PATCH def near far flags lod... — flags is token 4.
                in_physical = bool(int(tokens[4]) & 1)
            elif command == "END_PATCH":
                in_physical = False
            elif command == "BEGIN_PRIMITIVE":
                primitive_type = int(tokens[1])
                pool = []
            elif command == "PATCH_VERTEX":
                if in_physical:
                    pool.append(
                        (float(tokens[1]), float(tokens[2]), float(tokens[3]))
                    )
            elif command == "END_PRIMITIVE":
                if in_physical:
                    _flush()
                pool = []

    return numpy.array(triangles, dtype=numpy.float64) if triangles \
        else numpy.empty((0, 9), dtype=numpy.float64)


class AuthorMesh:
    """Barycentric elevation sampler over the parsed author base mesh.

    ``sample(longitude, latitude)`` returns the interpolated elevation, or
    ``None`` off the mesh or where the covering triangle is
    raster-referenced (sentinel corner).  ``local_datum`` gives the modal
    ring elevation, the A6 platform datum reference."""

    def __init__(self, triangles: numpy.ndarray) -> None:
        self._triangles = triangles
        self._corner_a = triangles[:, 0:3]
        self._corner_b = triangles[:, 3:6]
        self._corner_c = triangles[:, 6:9]
        longitudes_a, latitudes_a = self._corner_a[:, 0], self._corner_a[:, 1]
        longitudes_b, latitudes_b = self._corner_b[:, 0], self._corner_b[:, 1]
        longitudes_c, latitudes_c = self._corner_c[:, 0], self._corner_c[:, 1]
        self._denominator = (
            (latitudes_b - latitudes_c) * (longitudes_a - longitudes_c)
            + (longitudes_c - longitudes_b) * (latitudes_a - latitudes_c)
        )
        self._minimum_longitude = numpy.minimum.reduce(
            [longitudes_a, longitudes_b, longitudes_c])
        self._maximum_longitude = numpy.maximum.reduce(
            [longitudes_a, longitudes_b, longitudes_c])
        self._minimum_latitude = numpy.minimum.reduce(
            [latitudes_a, latitudes_b, latitudes_c])
        self._maximum_latitude = numpy.maximum.reduce(
            [latitudes_a, latitudes_b, latitudes_c])

    @property
    def triangle_count(self) -> int:
        return len(self._triangles)

    def all_vertices(self) -> numpy.ndarray:
        """Every corner as an ``(M, 3)`` ``(lon, lat, elev)`` array, sentinel
        rows dropped."""
        stacked = numpy.vstack(
            [self._corner_a, self._corner_b, self._corner_c])
        return stacked[stacked[:, 2] > RASTER_REFERENCE_SENTINEL]

    def sample(self, longitude: float, latitude: float) -> float | None:
        candidates = numpy.nonzero(
            (longitude >= self._minimum_longitude)
            & (longitude <= self._maximum_longitude)
            & (latitude >= self._minimum_latitude)
            & (latitude <= self._maximum_latitude)
            & (self._denominator != 0.0)
        )[0]
        for index in candidates:
            corner_a = self._corner_a[index]
            corner_b = self._corner_b[index]
            corner_c = self._corner_c[index]
            denominator = self._denominator[index]
            weight_a = (
                (corner_b[1] - corner_c[1]) * (longitude - corner_c[0])
                + (corner_c[0] - corner_b[0]) * (latitude - corner_c[1])
            ) / denominator
            weight_b = (
                (corner_c[1] - corner_a[1]) * (longitude - corner_c[0])
                + (corner_a[0] - corner_c[0]) * (latitude - corner_c[1])
            ) / denominator
            weight_c = 1.0 - weight_a - weight_b
            if weight_a >= -1e-9 and weight_b >= -1e-9 and weight_c >= -1e-9:
                if min(corner_a[2], corner_b[2], corner_c[2]) \
                        <= RASTER_REFERENCE_SENTINEL:
                    return None
                return float(
                    weight_a * corner_a[2]
                    + weight_b * corner_b[2]
                    + weight_c * corner_c[2]
                )
        return None

    def local_datum(self, polygon, margin_metres: float) -> float | None:
        """Modal elevation (whole metre) of a ring ``margin_metres`` outside
        the footprint's bounding box — the author's platform datum there."""
        minimum_longitude, minimum_latitude, maximum_longitude, maximum_latitude = (
            polygon.bounds
        )
        center_latitude = (minimum_latitude + maximum_latitude) / 2.0
        margin_degrees_latitude = margin_metres / 111320.0
        margin_degrees_longitude = margin_metres / (
            111320.0 * max(math.cos(math.radians(center_latitude)), 1e-6))
        values: list[float] = []
        steps = 16
        for step in range(steps):
            fraction = step / (steps - 1)
            longitude_at = minimum_longitude + fraction * (
                maximum_longitude - minimum_longitude)
            latitude_at = minimum_latitude + fraction * (
                maximum_latitude - minimum_latitude)
            for point in (
                (longitude_at, minimum_latitude - margin_degrees_latitude),
                (longitude_at, maximum_latitude + margin_degrees_latitude),
                (minimum_longitude - margin_degrees_longitude, latitude_at),
                (maximum_longitude + margin_degrees_longitude, latitude_at),
            ):
                elevation = self.sample(point[0], point[1])
                if elevation is not None:
                    values.append(round(elevation))
        if not values:
            return None
        return float(Counter(values).most_common(1)[0][0])


# ---------------------------------------------------------------------------
# Overlay classification (mirrors object_terrain_assembly) + structure set
# ---------------------------------------------------------------------------

def _default_xplane_root(dsf_path: str) -> str | None:
    current = os.path.abspath(dsf_path)
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return None
        if os.path.basename(parent) == "Custom Scenery":
            return os.path.dirname(parent)
        current = parent


def _resolve_dsf_path(argument: str) -> str | None:
    if os.path.isfile(argument):
        return argument
    earth_nav_data = os.path.join(argument, "Earth nav data")
    if os.path.isdir(earth_nav_data):
        for group in sorted(os.listdir(earth_nav_data)):
            group_dir = os.path.join(earth_nav_data, group)
            if not os.path.isdir(group_dir):
                continue
            for name in sorted(os.listdir(group_dir)):
                if name.lower().endswith(".dsf"):
                    return os.path.join(group_dir, name)
    return None


def _load_overlay(dsf_path: str, xplane_root: str | None):
    """Read placements + geometry and run the classifier; also return the
    geometry map and terrain placements for the below-grade building pass."""
    from shapely.geometry import Polygon

    lines = dsf_reader._load_dsf_text(dsf_path, cache_dir=tempfile.gettempdir())
    if lines is None:
        raise SystemExit("DSFTool could not read the overlay DSF")
    all_placements = obj8_reader.read_dsf_object_placements(
        lines,
        accept_resource=lambda resource: resource.lower().endswith(".obj"),
        include_object_msl=True,
    )
    mean_sea_level_placements = [
        placement for placement in all_placements
        if placement.placement_kind == "OBJECT_MSL"
    ]
    terrain_placements = [
        placement for placement in all_placements
        if placement.placement_kind != "OBJECT_MSL"
    ]
    pack_root = dsf_reader._pack_root_for_dsf(dsf_path) or ""

    placement_count: dict[str, int] = {}
    for placement in terrain_placements:
        placement_count[placement.resource_path] = (
            placement_count.get(placement.resource_path, 0) + 1)
    geometry_by_resource: dict = {}
    for resource_path in sorted(
            {placement.resource_path for placement in terrain_placements}):
        if placement_count[resource_path] > MAXIMUM_PLACEMENTS_PER_RESOURCE:
            continue
        physical_path = obj8_reader.resolve_object_resource(
            resource_path, pack_root, xplane_root)
        if physical_path is None:
            continue
        geometry = dsf_reader._load_object_geometry(physical_path)
        if geometry is None or not geometry.has_solid_geometry:
            continue
        geometry_by_resource[resource_path] = geometry

    pavement_polygons = None
    try:
        pavements = dsf_reader.read_dsf_pavements(
            dsf_path, cache_dir=tempfile.gettempdir(), xplane_root=xplane_root)
        pavement_polygons = [
            Polygon(outer_ring) for outer_ring, _holes, _def in pavements
            if len(outer_ring) >= 3
        ] or None
    except (OSError, ValueError):
        pass

    result = object_terrain_features.classify_object_terrain_features(
        terrain_placements, geometry_by_resource,
        pavement_polygons_longitude_latitude=pavement_polygons,
        mean_sea_level_placements=mean_sea_level_placements,
        pack_root=pack_root,
    )
    return result, terrain_placements, geometry_by_resource


def _below_grade_building_footprint(placement, geometry):
    """Union of the horizontal footprints (in lon/lat) of an object's
    solid triangles reaching at least :data:`BUILDING_BELOW_GRADE_M` below
    effective grade, or ``None`` if the object is not below grade."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    offset = placement.above_ground_level_metres
    below_grade_triangles = []
    for triangle in geometry.solid_triangles:
        heights = [
            offset + geometry.vertices[index][1] for index in triangle]
        if min(heights) <= -BUILDING_BELOW_GRADE_M:
            below_grade_triangles.append(triangle)
    if not below_grade_triangles:
        return None
    polygons = []
    for triangle in below_grade_triangles:
        ring = []
        for index in triangle:
            local_x, _local_y, local_z = geometry.vertices[index]
            latitude, longitude = obj8_reader.local_offset_to_lonlat(
                placement.latitude, placement.longitude,
                placement.heading_degrees, local_x, local_z)
            ring.append((longitude, latitude))
        if len(ring) >= 3:
            candidate = Polygon(ring)
            if candidate.is_valid and not candidate.is_empty:
                polygons.append(candidate)
    if not polygons:
        return None
    union = unary_union(polygons)
    if union.is_empty:
        return None
    if union.geom_type == "MultiPolygon":
        union = max(union.geoms, key=lambda part: part.area)
    return union


def _structure_footprint_longitude_latitude(structure, frame_polygon):
    """Convert a classifier structure-frame polygon to a lon/lat shapely
    polygon, or ``None``."""
    if frame_polygon is None or frame_polygon.is_empty:
        return None
    converted = frame_polygon_to_longitude_latitude(
        frame_polygon, structure.frame_origin_longitude_latitude)
    if converted.geom_type == "MultiPolygon":
        converted = max(converted.geoms, key=lambda part: part.area)
    return converted


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

def _grid_sample_interior(mesh, polygon, grid):
    """Sample the author mesh on a ``grid x grid`` lattice over the polygon
    bounding box, keeping points inside the polygon.  Returns the list of
    (elevation) values that landed on the mesh (non-sentinel)."""
    from shapely.geometry import Point

    minimum_longitude, minimum_latitude, maximum_longitude, maximum_latitude = (
        polygon.bounds)
    values = []
    interior_points = 0
    for row in range(grid):
        for column in range(grid):
            longitude = minimum_longitude + (column + 0.5) / grid * (
                maximum_longitude - minimum_longitude)
            latitude = minimum_latitude + (row + 0.5) / grid * (
                maximum_latitude - minimum_latitude)
            if not polygon.contains(Point(longitude, latitude)):
                continue
            interior_points += 1
            elevation = mesh.sample(longitude, latitude)
            if elevation is not None:
                values.append(elevation)
    return values, interior_points


def _verdict(mesh, polygon, grid, margin_metres):
    """Local datum, EXPOSED/PARTIAL/BURIED verdict, floor and dip fraction
    for one footprint."""
    datum = mesh.local_datum(polygon, margin_metres)
    values, interior_points = _grid_sample_interior(mesh, polygon, grid)
    if datum is None or not values:
        return {
            "datum": datum, "verdict": "NO-DATA", "floor": None,
            "depth": None, "dip_fraction": None,
            "samples": len(values), "interior": interior_points,
        }
    dipped = [value for value in values if value <= datum - DIP_BELOW_DATUM_M]
    dip_fraction = len(dipped) / len(values)
    floor = min(values)
    if dip_fraction >= EXPOSED_FOOTPRINT_FRACTION:
        verdict = "EXPOSED"
    elif dip_fraction >= PARTIAL_FOOTPRINT_FRACTION:
        verdict = "PARTIAL"
    else:
        verdict = "BURIED"
    return {
        "datum": datum, "verdict": verdict, "floor": floor,
        "depth": datum - floor, "dip_fraction": dip_fraction,
        "samples": len(values), "interior": interior_points,
        "cutout_cells": len(dipped),
    }


def _print_verdict(label, resources, verdict) -> None:
    datum = verdict["datum"]
    datum_text = f"{datum:.0f} m" if datum is not None else "n/a"
    if verdict["verdict"] == "NO-DATA":
        print(f"  {label}: datum {datum_text}  verdict NO-DATA "
              f"({verdict['samples']} samples / {verdict['interior']} interior "
              f"points on mesh)")
        return
    print(f"  {label}: datum {datum_text}  {verdict['verdict']}  "
          f"floor {verdict['floor']:.1f} m  depth {verdict['depth']:.1f} m  "
          f"dip {verdict['dip_fraction'] * 100:.0f}% of "
          f"{verdict['samples']} samples")
    if verdict["verdict"] == "EXPOSED":
        print(f"      author cutout: {verdict['cutout_cells']} sub-datum "
              f"cells of {verdict['samples']} sampled")
    print(f"      resources: {', '.join(resources[:6])}"
          + (" ..." if len(resources) > 6 else ""))


def _run_structure_pass(mesh, result, terrain_placements,
                        geometry_by_resource, grid, margin_metres):
    print("=== TUNNELS (author-mesh verdict) ===")
    for index, tunnel in enumerate(result.tunnels):
        polygon = _structure_footprint_longitude_latitude(
            tunnel, tunnel.deck_footprint)
        if polygon is None:
            print(f"  [tunnel {index}]: no deck footprint")
            continue
        verdict = _verdict(mesh, polygon, grid, margin_metres)
        _print_verdict(f"[tunnel {index}]", tunnel.object_resources, verdict)

    print("\n=== BRIDGES (author-mesh verdict; resolves 4.obj) ===")
    for index, bridge in enumerate(result.bridges):
        polygon = _structure_footprint_longitude_latitude(
            bridge, bridge.deck_polygon)
        if polygon is None:
            print(f"  [bridge {index}]: no deck polygon")
            continue
        verdict = _verdict(mesh, polygon, grid, margin_metres)
        _print_verdict(f"[bridge {index}] ({bridge.contract})",
                       bridge.object_resources, verdict)

    print("\n=== BELOW-GRADE BUILDINGS (author-mesh verdict) ===")
    consumed = {resource for _pack, resource in result.exclusions}
    first_placement: dict = {}
    for placement in terrain_placements:
        first_placement.setdefault(placement.resource_path, placement)
    buried = exposed = partial = 0
    for resource_path in sorted(geometry_by_resource):
        if resource_path in consumed:
            continue
        geometry = geometry_by_resource[resource_path]
        placement = first_placement[resource_path]
        polygon = _below_grade_building_footprint(placement, geometry)
        if polygon is None or polygon.area <= 0:
            continue
        verdict = _verdict(mesh, polygon, grid, margin_metres)
        if verdict["verdict"] == "BURIED":
            buried += 1
        elif verdict["verdict"] == "EXPOSED":
            exposed += 1
            _print_verdict("[below-grade building]", [resource_path], verdict)
        elif verdict["verdict"] == "PARTIAL":
            partial += 1
            _print_verdict("[below-grade building]", [resource_path], verdict)
    print(f"  summary: {buried} BURIED, {partial} PARTIAL, {exposed} EXPOSED "
          f"(A6 expectation: all below-grade buildings BURIED)")


def _run_depressions_pass(mesh, result, terrain_placements,
                          geometry_by_resource, margin_metres):
    """A6 reverse pass: cluster author-mesh vertices below local datum and
    report enclosed interior depressions with the overlay structures over
    each (the KDEN interior-hall analogue)."""
    from shapely.geometry import Polygon

    vertices = mesh.all_vertices()
    if not len(vertices):
        print("no non-sentinel author-mesh vertices")
        return

    # Coarse modal-datum grid so "below datum" is local, not global.
    datum_cell = 0.003  # ~200-330 m
    modal_by_cell: dict[tuple[int, int], float] = {}
    bucket: dict[tuple[int, int], list[float]] = defaultdict(list)
    for longitude, latitude, elevation in vertices:
        bucket[(int(longitude / datum_cell), int(latitude / datum_cell))
               ].append(round(elevation))
    for cell, elevations in bucket.items():
        modal_by_cell[cell] = Counter(elevations).most_common(1)[0][0]

    def datum_at(longitude, latitude) -> float:
        cell = (int(longitude / datum_cell), int(latitude / datum_cell))
        best = modal_by_cell.get(cell)
        return float(best) if best is not None else 0.0

    depressed = numpy.array([
        (longitude, latitude, elevation)
        for longitude, latitude, elevation in vertices
        if elevation <= datum_at(longitude, latitude) - DEPRESSION_MIN_DEPTH_M
    ])
    print(f"depressed vertices (>= {DEPRESSION_MIN_DEPTH_M:.0f} m below local "
          f"datum): {len(depressed)}")
    if not len(depressed):
        return

    cell = 0.0005
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, (longitude, latitude, _elevation) in enumerate(depressed):
        cells[(int(longitude / cell), int(latitude / cell))].append(index)
    occupied = list(cells)
    index_of = {occupied_cell: position
                for position, occupied_cell in enumerate(occupied)}
    parent = list(range(len(occupied)))

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for occupied_cell in occupied:
        for delta_x in (-1, 0, 1):
            for delta_y in (-1, 0, 1):
                neighbour = (occupied_cell[0] + delta_x,
                             occupied_cell[1] + delta_y)
                if neighbour in index_of:
                    root_a = find(index_of[occupied_cell])
                    root_b = find(index_of[neighbour])
                    if root_a != root_b:
                        parent[root_a] = root_b
    groups: dict[int, list] = defaultdict(list)
    for occupied_cell in occupied:
        groups[find(index_of[occupied_cell])].append(occupied_cell)

    # Tunnel footprints (to label a depression as a tunnel trench).
    tunnel_polygons = []
    for tunnel in result.tunnels:
        polygon = _structure_footprint_longitude_latitude(
            tunnel, tunnel.deck_footprint)
        if polygon is not None:
            tunnel_polygons.append(polygon)

    # Overlay object bounding boxes for the "what sits over it" report.
    first_placement: dict = {}
    for placement in terrain_placements:
        first_placement.setdefault(placement.resource_path, placement)
    object_boxes = []
    for resource_path, geometry in geometry_by_resource.items():
        placement = first_placement[resource_path]
        longitudes = []
        latitudes = []
        used = {index for triangle in geometry.solid_triangles
                for index in triangle}
        for index in used:
            local_x, _local_y, local_z = geometry.vertices[index]
            latitude, longitude = obj8_reader.local_offset_to_lonlat(
                placement.latitude, placement.longitude,
                placement.heading_degrees, local_x, local_z)
            longitudes.append(longitude)
            latitudes.append(latitude)
        if longitudes:
            object_boxes.append((
                resource_path,
                (min(longitudes), min(latitudes),
                 max(longitudes), max(latitudes))))

    print("\nENCLOSED interior depressions (candidate author cutouts):\n")
    reported = 0
    for group_cells in groups.values():
        indices = [i for group_cell in group_cells for i in cells[group_cell]]
        points = depressed[indices]
        minimum_longitude, maximum_longitude = (
            points[:, 0].min(), points[:, 0].max())
        minimum_latitude, maximum_latitude = (
            points[:, 1].min(), points[:, 1].max())
        floor = points[:, 2].min()
        center_longitude = (minimum_longitude + maximum_longitude) / 2.0
        center_latitude = (minimum_latitude + maximum_latitude) / 2.0
        metres_per_degree_longitude = 111320.0 * math.cos(
            math.radians(center_latitude))
        area = len(set(group_cells)) * (cell * metres_per_degree_longitude) * (
            cell * 111320.0)
        if area < DEPRESSION_MIN_AREA_M2:
            continue
        box = Polygon([
            (minimum_longitude, minimum_latitude),
            (maximum_longitude, minimum_latitude),
            (maximum_longitude, maximum_latitude),
            (minimum_longitude, maximum_latitude)])
        is_tunnel = any(polygon.intersects(box) for polygon in tunnel_polygons)
        datum = datum_at(center_longitude, center_latitude)
        overlay = sorted({
            resource_path for resource_path, bounds in object_boxes
            if not (bounds[2] < minimum_longitude
                    or bounds[0] > maximum_longitude
                    or bounds[3] < minimum_latitude
                    or bounds[1] > maximum_latitude)})
        tag = "TUNNEL-TRENCH" if is_tunnel else "INTERIOR-DEPRESSION"
        print(f"  [{tag}] area~{area:.0f} m2  floor {floor:.0f} m  "
              f"datum {datum:.0f} m  depth {datum - floor:.0f} m  "
              f"@({center_latitude:.5f},{center_longitude:.5f})")
        if overlay:
            print(f"      overlay objects over it: "
                  f"{', '.join(overlay[:8])}"
                  + (" ..." if len(overlay) > 8 else ""))
        reported += 1
    print(f"\n{reported} depressions >= {DEPRESSION_MIN_AREA_M2:.0f} m2")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("author_mesh",
                        help="author base-mesh DSF (or pre-dumped .txt)")
    parser.add_argument("overlay",
                        help="overlay tile DSF OR overlay pack root")
    parser.add_argument("--xplane-root", default=None)
    parser.add_argument("--depressions", action="store_true",
                        help="run the A6 reverse pass (enclosed depressions)")
    parser.add_argument("--grid", type=int, default=40,
                        help="interior sampling lattice per footprint")
    parser.add_argument("--margin-metres", type=float,
                        default=DATUM_RING_MARGIN_M)
    args = parser.parse_args()

    overlay_dsf = _resolve_dsf_path(args.overlay)
    if overlay_dsf is None:
        print(f"no overlay DSF at or under {args.overlay}", file=sys.stderr)
        return 2
    xplane_root = args.xplane_root or _default_xplane_root(overlay_dsf)

    result, terrain_placements, geometry_by_resource = _load_overlay(
        overlay_dsf, xplane_root)

    # Airport bounding box from the terrain placements, with margin, to bound
    # the (very large) mesh parse.
    longitudes = [placement.longitude for placement in terrain_placements]
    latitudes = [placement.latitude for placement in terrain_placements]
    margin_degrees = 0.02
    bounds = (min(longitudes) - margin_degrees,
              min(latitudes) - margin_degrees,
              max(longitudes) + margin_degrees,
              max(latitudes) + margin_degrees)

    text_path = _dump_mesh_text(args.author_mesh)
    print(f"parsing author mesh: {args.author_mesh}")
    triangles = parse_author_mesh_triangles(text_path, bounds)
    mesh = AuthorMesh(triangles)
    print(f"author physical triangles in airport bbox: {mesh.triangle_count}")
    print(f"overlay: {overlay_dsf}")
    print(f"classified: {len(result.tunnels)} tunnels, "
          f"{len(result.bridges)} bridges, {len(result.refusals)} refused\n")
    if not mesh.triangle_count:
        print("no author-mesh triangles over the airport — wrong tile?")
        return 1

    if args.depressions:
        _run_depressions_pass(mesh, result, terrain_placements,
                              geometry_by_resource, args.margin_metres)
    else:
        _run_structure_pass(mesh, result, terrain_placements,
                            geometry_by_resource, args.grid, args.margin_metres)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
