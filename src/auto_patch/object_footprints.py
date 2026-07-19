"""OBJ8 structure footprints — Phase 1 of the DSF object integration.

Contract frozen by workstream W1 (``docs/dsf_object_integration_spec.md``
sections 3 and 4-W6); implementation lands in workstream W6.

Turns a partitioned :class:`~auto_patch.object_anchor.Structure` into a
building footprint ring so auto_patch's pad grading, clearance and
terminal attraction can see buildings it is blind to today (roughly 105
at KCLT).  Rulings in force:

* R3 — convex hull ships as the default ring; the faithful
  triangle-union ring sits behind ``DSF_OBJECT_FOOTPRINT_UNION`` until
  the pad-grading interaction is measured on the gate airports.
* R4 — footprints are ADDITIVE: they enter the existing DSF building
  pool (role ``"object"``), where ``_cluster_dsf_building_facades``
  unions any overlap with ``.fac`` facades and
  ``_combine_building_sources`` drops OSM duplicates.  No new overlap
  predicate (spec section 2.3).
* R5 — buildings are FLAT; footprints obey it.

Phase 1 never touches the mesh, and — amendment A1 — it MUST use the same
contact-graph partition as Phase 2: a pad flattened under a
differently-partitioned structure is not flat under the structure the
y offset seats (spec section 7.3).
"""

from __future__ import annotations

import math

from shapely.geometry import MultiPoint, Polygon
from shapely.ops import unary_union

import O4_UI_Utils as UI

# Imported as modules (not names) so tests can monkeypatch the
# projection while workstream W2's implementation is still landing, and
# so the real implementation is picked up the moment it does.
from . import obj8_reader
from .obj8_reader import ObjectGeometry, ObjectPlacement
from .object_anchor import Structure

# Douglas-Peucker tolerance for the triangle-union ring, in degrees of
# longitude/latitude (~0.2 m at mid latitudes).  The union of thousands
# of projected triangles carries collinear seam vertices that would
# bloat the building pool for no geometric information; the hull path
# never needs simplification.
FOOTPRINT_SIMPLIFY_TOLERANCE_DEGREES = 2.0e-6

# Exceptions the geometry combinators here may legitimately raise on
# degenerate input (shapely domain only — never built-ins that would
# mask a real bug).
try:  # shapely 2
    from shapely.errors import GEOSException as _GEOS_EXCEPTION
except ImportError:  # pragma: no cover - shapely 1 fallback
    from shapely.errors import TopologicalError as _GEOS_EXCEPTION


def _footprint_area_square_metres(footprint_lonlat: Polygon) -> float:
    """Approximate metric area of a small lon/lat polygon (local
    equirectangular scale at the polygon's own latitude — exactly the
    projection ``obj8_reader.local_offset_to_lonlat`` inverts)."""
    centroid_latitude = footprint_lonlat.centroid.y
    metres_per_degree_longitude = (
        obj8_reader.METRES_PER_DEGREE_LATITUDE
        * math.cos(math.radians(centroid_latitude)))
    return (footprint_lonlat.area
            * obj8_reader.METRES_PER_DEGREE_LATITUDE
            * metres_per_degree_longitude)


def _footprint_span_metres(footprint_lonlat: Polygon) -> float:
    """Larger side of the metric bounding box of a small lon/lat polygon
    (same local equirectangular scale as ``_footprint_area_square_metres``)
    — the structure span gate's metric (defect 2026-07-17)."""
    centroid_latitude = footprint_lonlat.centroid.y
    metres_per_degree_longitude = (
        obj8_reader.METRES_PER_DEGREE_LATITUDE
        * math.cos(math.radians(centroid_latitude)))
    minimum_longitude, minimum_latitude, maximum_longitude, maximum_latitude = (
        footprint_lonlat.bounds)
    return max(
        (maximum_longitude - minimum_longitude) * metres_per_degree_longitude,
        (maximum_latitude - minimum_latitude)
        * obj8_reader.METRES_PER_DEGREE_LATITUDE)


def _triangle_union_footprint(
    triangle_corner_points: list[tuple[tuple[float, float],
                                       tuple[float, float],
                                       tuple[float, float]]],
) -> Polygon | None:
    """The ``DSF_OBJECT_FOOTPRINT_UNION`` ring: unary_union of the
    projected triangles, ``buffer(0)``-repaired, exterior only (interior
    rings are dropped in v1), DP-simplified.  Returns ``None`` when the
    union degenerates."""
    triangle_polygons = []
    for corner_points in triangle_corner_points:
        try:
            triangle_polygon = Polygon(corner_points)
            if not triangle_polygon.is_valid:
                triangle_polygon = triangle_polygon.buffer(0)
            if (not triangle_polygon.is_empty
                    and triangle_polygon.geom_type == "Polygon"
                    and triangle_polygon.area > 0.0):
                triangle_polygons.append(triangle_polygon)
        except (ValueError, _GEOS_EXCEPTION):
            continue
    if not triangle_polygons:
        return None
    try:
        union = unary_union(triangle_polygons)
        if not union.is_valid:
            union = union.buffer(0)
    except (ValueError, _GEOS_EXCEPTION):
        return None
    if union.geom_type == "MultiPolygon":
        # Base-filtered triangles of one structure can form several
        # disjoint ground patches (for example separate wall footings);
        # one structure gets one pad, so keep the dominant patch.
        union = max(union.geoms, key=lambda geometry: geometry.area)
    if union.is_empty or union.geom_type != "Polygon":
        return None
    exterior_only = Polygon(union.exterior)
    try:
        simplified = exterior_only.simplify(
            FOOTPRINT_SIMPLIFY_TOLERANCE_DEGREES, preserve_topology=True)
    except (ValueError, _GEOS_EXCEPTION):
        simplified = exterior_only
    if simplified.is_empty or simplified.geom_type != "Polygon":
        simplified = exterior_only
    return simplified


def foot_pad_ring(
    contact_points_lonlat: list[tuple[float, float]],
    margin_metres: float,
) -> list[tuple[float, float]] | None:
    """Build a small terrain-pad ring around ONE foot cluster's contact
    points (multi-ground-cluster re-anchor; the points come from
    ``object_anchor.FootPadRequest.contact_points_lonlat``).

    Same contract as :func:`structure_ring` — ``(longitude, latitude)``,
    unclosed — but per FOOT, not per structure: the convex hull of the
    contact points, dilated by ``margin_metres`` so the pad reaches past
    the very edge of the foot.  Hull and dilation run in local metres at
    the cluster's own latitude (the projection
    ``obj8_reader.local_offset_to_lonlat`` inverts), then map back.
    Returns ``None`` when the input degenerates.
    """
    if not contact_points_lonlat:
        return None
    centroid_latitude = sum(
        latitude for _longitude, latitude in contact_points_lonlat
    ) / len(contact_points_lonlat)
    metres_per_degree_longitude = (
        obj8_reader.METRES_PER_DEGREE_LATITUDE
        * math.cos(math.radians(centroid_latitude)))
    if metres_per_degree_longitude <= 0.0:
        return None
    centroid_longitude = sum(
        longitude for longitude, _latitude in contact_points_lonlat
    ) / len(contact_points_lonlat)
    local_points = [
        ((longitude - centroid_longitude) * metres_per_degree_longitude,
         (latitude - centroid_latitude)
         * obj8_reader.METRES_PER_DEGREE_LATITUDE)
        for longitude, latitude in contact_points_lonlat]
    try:
        padded = MultiPoint(local_points).convex_hull.buffer(
            margin_metres, quad_segs=2)
    except (ValueError, _GEOS_EXCEPTION):
        return None
    if padded.is_empty or padded.geom_type != "Polygon":
        return None
    ring = [
        (float(centroid_longitude + x / metres_per_degree_longitude),
         float(centroid_latitude
               + y / obj8_reader.METRES_PER_DEGREE_LATITUDE))
        for x, y in padded.exterior.coords[:-1]]
    return ring if len(ring) >= 3 else None


def draped_pavement_patches(
    geometry: ObjectGeometry,
    placement: ObjectPlacement,
    minimum_patch_area_square_metres: float,
) -> list[tuple[list[tuple[float, float]],
                list[list[tuple[float, float]]]]]:
    """Union one placement's DRAPED triangles into pavement patches.

    The object-pavement source (HECA Tai Models,
    ``dsf_reader.read_dsf_object_pavements``): a ground-paint ``.obj``
    carries the whole airport's geometry for one texture, so its draped
    triangles union into MANY disjoint pavement areas.  Unlike the
    building-pad ring (:func:`structure_ring` — one structure, one
    dominant patch, holes dropped), pavement keeps EVERY patch above
    ``minimum_patch_area_square_metres`` and honours interior rings: a
    perforated apron sheet must not fill its infield holes.

    Returns ``(outer_ring, hole_rings)`` pairs in ``(longitude,
    latitude)``, rings unclosed — the ``read_dsf_pavements`` ring
    contract, so the caller can feed both sources through one path.
    Returns ``[]`` when the geometry has no draped triangles or the
    union degenerates.
    """
    if not geometry.draped_triangles:
        return []
    projected_by_vertex_index: dict[int, tuple[float, float]] = {}
    triangle_polygons = []
    for triangle in geometry.draped_triangles:
        corner_points = []
        for vertex_index in triangle:
            point = projected_by_vertex_index.get(vertex_index)
            if point is None:
                local_x, _local_y, local_z = (
                    geometry.vertices[vertex_index])
                latitude, longitude = obj8_reader.local_offset_to_lonlat(
                    placement.latitude,
                    placement.longitude,
                    placement.heading_degrees,
                    local_x,
                    local_z,
                )
                point = (longitude, latitude)
                projected_by_vertex_index[vertex_index] = point
            corner_points.append(point)
        try:
            triangle_polygon = Polygon(corner_points)
            if not triangle_polygon.is_valid:
                triangle_polygon = triangle_polygon.buffer(0)
            if (not triangle_polygon.is_empty
                    and triangle_polygon.geom_type == "Polygon"
                    and triangle_polygon.area > 0.0):
                triangle_polygons.append(triangle_polygon)
        except (ValueError, _GEOS_EXCEPTION):
            continue
    if not triangle_polygons:
        return []
    try:
        union = unary_union(triangle_polygons)
        if not union.is_valid:
            union = union.buffer(0)
    except (ValueError, _GEOS_EXCEPTION):
        return []
    if union.is_empty:
        return []
    patches = list(union.geoms) if union.geom_type == "MultiPolygon" else (
        [union] if union.geom_type == "Polygon" else [])

    out: list[tuple[list[tuple[float, float]],
                    list[list[tuple[float, float]]]]] = []
    for patch in patches:
        if (_footprint_area_square_metres(patch)
                < minimum_patch_area_square_metres):
            continue
        try:
            simplified = patch.simplify(
                FOOTPRINT_SIMPLIFY_TOLERANCE_DEGREES,
                preserve_topology=True)
        except (ValueError, _GEOS_EXCEPTION):
            simplified = patch
        if simplified.is_empty or simplified.geom_type != "Polygon":
            simplified = patch
        outer_ring = [(float(longitude), float(latitude))
                      for longitude, latitude
                      in simplified.exterior.coords[:-1]]
        if len(outer_ring) < 3:
            continue
        hole_rings = []
        for interior in simplified.interiors:
            hole_ring = [(float(longitude), float(latitude))
                         for longitude, latitude in interior.coords[:-1]]
            if len(hole_ring) >= 3:
                hole_rings.append(hole_ring)
        out.append((outer_ring, hole_rings))
    return out


def is_vehicle_pavement_patch(
    patch_polygon_metres,
    minimum_aircraft_width_metres: float,
    opening_area_ratio: float,
) -> bool:
    """True when a ground-paint pavement patch is vehicle/drainage paint
    rather than aircraft-capable pavement (owner direction 2026-07-18).

    The test is a morphological OPENING RATIO: erode the patch by half
    ``minimum_aircraft_width_metres``, dilate back, and compare the
    surviving area to the original.  A painted service road or drainage
    channel is a long corridor narrower than any aircraft pavement, so
    almost nothing survives — but plain erosion-to-empty is NOT enough:
    a road NETWORK patch has occasional wide pockets (intersections,
    small plazas) that survive erosion and would keep the whole
    connected snake (measured: HECA ``road.obj``'s 165,820 m² patch,
    ~6 m corridors over a 2.7 × 7.4 km span, survived plain erosion).
    Opening recovers the aircraft-capable cores at full extent, and the
    area fraction separates cleanly (HECA: vehicle/drainage ≤ 0.29,
    real pavement ≥ 0.37, bulk ≥ 0.96).

    The caller gates on ``minimum_aircraft_width_metres > 0`` (0 =
    filter disabled) and on the patch being OBJECT-sourced.  Geometry
    errors classify as NOT vehicle (keep the patch — admission must
    fail open, never silently drop real pavement).
    """
    try:
        half_width = 0.5 * minimum_aircraft_width_metres
        eroded = patch_polygon_metres.buffer(-half_width)
        opened_area = (0.0 if eroded.is_empty
                       else eroded.buffer(half_width).area)
        return opened_area < opening_area_ratio * patch_polygon_metres.area
    except (ValueError, _GEOS_EXCEPTION):
        return False


def abutting_contact_ratio(
    patch_polygon_metres,
    neighbour_polygons_metres,
    contact_tolerance_metres: float = 0.5,
) -> float:
    """Fraction of a patch's LONG side in edge-contact with neighbour
    pavement: shared-boundary length / half the patch perimeter.

    A taxiway SHOULDER abuts the pavement it serves for its whole run
    (ratio ~1.0 one-sided, ~2.0 sandwiched between two pavements); a
    perimeter road or an offset strip only meets pavement at crossings
    (measured HECA: roads <= 0.32, abutting strips >= 0.58 — a clean
    gap).  Used by the pipeline's vehicle-pavement admission filter to
    READMIT narrow object patches that are really shoulders.  Geometry
    errors return 0.0 (no readmission claim).
    """
    try:
        exterior = patch_polygon_metres.exterior
        half_perimeter = 0.5 * exterior.length
        if half_perimeter <= 0.0:
            return 0.0
        contact_length = 0.0
        for neighbour in neighbour_polygons_metres:
            try:
                contact_length += exterior.intersection(
                    neighbour.buffer(contact_tolerance_metres)).length
            except (ValueError, _GEOS_EXCEPTION):
                continue
        return contact_length / half_perimeter
    except (ValueError, _GEOS_EXCEPTION):
        return 0.0


def structure_ring(
    structure: Structure,
    geometry_by_resource: dict[str, ObjectGeometry],
    placements: list[ObjectPlacement],
) -> list[tuple[float, float]] | None:
    """Build a footprint ring for one structure, in ``(longitude,
    latitude)``, unclosed (first vertex not repeated) — matching
    ``read_dsf_buildings``'s existing building-tuple contract.

    * Take solid vertices with ``y <= minimum_base_y +
      DSF_OBJECT_FOOTPRINT_HEIGHT_M`` (roof overhang must not inflate the
      pad); fewer than 3 qualifying falls back to all solid vertices.
    * Project each vertex through its OWN object's placement.
    * Ring: convex hull by default; under ``DSF_OBJECT_FOOTPRINT_UNION``,
      the buffer(0)-repaired union of the projected triangles, exterior
      only, simplified.
    * Return ``None`` for a structure with no ground-touching part
      (rooftop clutter is not a building pad), and — reported, never
      silent — above ``DSF_OBJECT_MAX_FOOTPRINT_AREA_M2`` when that cap
      is enabled.
    """
    # Function-local flag imports so tests can monkeypatch the config
    # module (the module-level idiom freezes the value — spec section
    # 4-W1, "one trap").
    from .config import (
        DSF_OBJECT_FOOTPRINT_HEIGHT_M,
        DSF_OBJECT_FOOTPRINT_UNION,
        DSF_OBJECT_MAX_FOOTPRINT_AREA_M2,
        DSF_OBJECT_MAX_STRUCTURE_SPAN_M,
        DSF_OBJECT_MIN_BUILDING_HEIGHT_M,
    )

    if not structure.is_ground_touching:
        return None
    if (not structure.triangles_by_resource
            or not structure.minimum_base_y_by_resource):
        return None

    placement_by_resource = {
        placement.resource_path: placement for placement in placements}
    minimum_base_y = min(structure.minimum_base_y_by_resource.values())
    # The footprint band reaches from the lowest solid vertex up to
    # GRADE plus the band height — never just ``minimum + height``.  A
    # below-grade pocket (KBNA terminal: a basement piece at authored
    # y = -2.85, a garage level at -23) would otherwise pull the whole
    # band under grade, and the "footprint" of a 780,000 m² terminal
    # complex collapses to the 2 m hull of its deepest basement
    # (found 2026-07-14).  For structures based at or above grade
    # (including baked-offset feet at +6.5) this is exactly the old
    # ``minimum + height`` band.
    base_ceiling_y = (
        max(minimum_base_y, 0.0) + DSF_OBJECT_FOOTPRINT_HEIGHT_M)

    base_points: list[tuple[float, float]] = []
    all_points: list[tuple[float, float]] = []
    base_triangle_corner_points: list = []
    all_triangle_corner_points: list = []
    maximum_local_y = minimum_base_y

    for resource_path, triangles in structure.triangles_by_resource.items():
        geometry = geometry_by_resource.get(resource_path)
        placement = placement_by_resource.get(resource_path)
        if geometry is None or placement is None or not triangles:
            continue
        # Project each vertex through its OWN object's placement (spec
        # section 2.4: the anchor is a per-object property).
        projected_by_vertex_index: dict[int, tuple[float, float]] = {}
        is_base_vertex: dict[int, bool] = {}
        for triangle in triangles:
            for vertex_index in triangle:
                if vertex_index in projected_by_vertex_index:
                    continue
                local_x, local_y, local_z = geometry.vertices[vertex_index]
                if local_y > maximum_local_y:
                    maximum_local_y = local_y
                latitude, longitude = obj8_reader.local_offset_to_lonlat(
                    placement.latitude,
                    placement.longitude,
                    placement.heading_degrees,
                    local_x,
                    local_z,
                )
                point = (longitude, latitude)
                projected_by_vertex_index[vertex_index] = point
                is_base_vertex[vertex_index] = local_y <= base_ceiling_y
                all_points.append(point)
                if is_base_vertex[vertex_index]:
                    base_points.append(point)
        if DSF_OBJECT_FOOTPRINT_UNION:
            for triangle in triangles:
                corner_points = tuple(
                    projected_by_vertex_index[vertex_index]
                    for vertex_index in triangle)
                all_triangle_corner_points.append(corner_points)
                if all(is_base_vertex[vertex_index]
                       for vertex_index in triangle):
                    base_triangle_corner_points.append(corner_points)

    if len(all_points) < 3:
        return None
    # Amendment A11 (from the HECA Tai Models pack): a building has
    # walls; a ground plate, sign or decal does not.  A near-flat
    # structure gets NO Phase-1 pad — Phase 2 still y-bakes it, since a
    # mis-elevated ground plate is exactly a float/sink artifact.
    # HECA's ``heca_ground_polygon.obj`` spans 2.1 km and must never
    # become a 2 km flat building pad.
    if (DSF_OBJECT_MIN_BUILDING_HEIGHT_M > 0.0
            and (maximum_local_y - minimum_base_y)
            < DSF_OBJECT_MIN_BUILDING_HEIGHT_M):
        UI.vprint(
            2,
            "  [object-footprints] structure vertical extent "
            f"{maximum_local_y - minimum_base_y:.2f} m is below the "
            f"{DSF_OBJECT_MIN_BUILDING_HEIGHT_M:.2f} m building floor "
            "(O4_DSF_OBJECT_MIN_BUILDING_HEIGHT_M) — ground plate or "
            "decal, no pad.")
        return None
    # Fewer than 3 base vertices → all solid vertices (low flat objects
    # authored entirely above the height window).
    use_base_vertices = len(base_points) >= 3

    footprint: Polygon | None
    if DSF_OBJECT_FOOTPRINT_UNION:
        footprint = _triangle_union_footprint(
            base_triangle_corner_points if use_base_vertices
            else all_triangle_corner_points)
    else:
        try:
            hull = MultiPoint(
                base_points if use_base_vertices else all_points
            ).convex_hull
        except (ValueError, _GEOS_EXCEPTION):
            return None
        footprint = hull if (not hull.is_empty
                             and hull.geom_type == "Polygon") else None
    if footprint is None:
        return None

    # Structure span gate (defect 2026-07-17): a structure whose footprint
    # ring spans past the cap is a residual field-spanning hull the
    # connector pre-filter did not fully un-chain — never a building-pad
    # seed.  Skip-and-report through the same path as the area cap.
    if DSF_OBJECT_MAX_STRUCTURE_SPAN_M > 0.0:
        span_metres = _footprint_span_metres(footprint)
        if span_metres > DSF_OBJECT_MAX_STRUCTURE_SPAN_M:
            UI.vprint(
                1,
                "  [object-footprints] structure footprint span "
                f"{span_metres:.0f} m exceeds the "
                f"{DSF_OBJECT_MAX_STRUCTURE_SPAN_M:.0f} m structure span "
                "gate (O4_DSF_OBJECT_MAX_STRUCTURE_SPAN_M) — skipped; a "
                "field-spanning structure is not a building pad.")
            return None

    if DSF_OBJECT_MAX_FOOTPRINT_AREA_M2 > 0.0:
        area_square_metres = _footprint_area_square_metres(footprint)
        if area_square_metres > DSF_OBJECT_MAX_FOOTPRINT_AREA_M2:
            # Skip-and-report, never a quiet clip (spec section 2.3).
            UI.vprint(
                1,
                "  [object-footprints] structure footprint "
                f"{area_square_metres:.0f} m2 exceeds the "
                f"{DSF_OBJECT_MAX_FOOTPRINT_AREA_M2:.0f} m2 cap "
                "(O4_DSF_OBJECT_MAX_FOOTPRINT_AREA_M2) — skipped; "
                "the structure needs a pad review.")
            return None

    ring = [(float(longitude), float(latitude))
            for longitude, latitude in footprint.exterior.coords[:-1]]
    return ring if len(ring) >= 3 else None
