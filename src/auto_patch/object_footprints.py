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

from .obj8_reader import ObjectGeometry, ObjectPlacement
from .object_anchor import Structure


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
    raise NotImplementedError("workstream W6")
