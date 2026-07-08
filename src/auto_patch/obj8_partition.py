"""Partition pooled OBJ8 geometry into structures via the contact graph.

Contract frozen by workstream W1 (amendment A1 of
``docs/dsf_object_integration_spec.md``); implementation lands in
workstream W2.  The theory, the measurements, and the traps are in
``docs/obj8_structure_partition.md`` — read it in full before touching
this module.

The one-paragraph version.  With the correction
``delta(S, O) = ground_under(centroid(S)) - ground_under(anchor(O))``
applied to every vertex of structure ``S`` contributed by object ``O``,
the anchor term cancels at render time, so every structure moves as a
rigid body under pure vertical translation and its internal assembly is
preserved exactly.  All distortion therefore lives on structure
boundaries, which makes the optimal partition exactly the connected
components of the epsilon-contact graph: any coarser partition pays
residual for nothing, any finer partition tears contacting parts apart.
The only parameter is epsilon — a modelling tolerance ("how large a gap
did the author leave between a wall and its roof"), measured on a plateau
of 0.02–0.25 m at KCLT (``DSF_OBJECT_CONTACT_EPSILON_M``).

The trap (do not "fix" this): vertex-connectivity is NOT contact.  These
bakes are triangle soup — a wall abuts the roof it holds up without
sharing a vertex.  Partitioning on vertex-connectivity alone measured
4,453 torn abutments up to 11.39 m at KCLT.  Contact needs a broad phase
(3D axis-aligned bounding boxes, which is sound: box gap never exceeds
surface gap, so it can over-merge but never tear) and a narrow phase
(surface distance, pruning 43% of the broad-phase edges at KCLT).  A pair
whose contact cannot be PROVED absent keeps its edge — tearing is the
unrecoverable failure, over-merging costs centimetres (invariant I-20).

Frames: callers pool geometry across objects in AUTHORED space — world
XZ through each object's own placement, projected into one local
east-north-up frame centred on the pool, with y left as the authored
``v.y`` (never ``terrain(anchor) + v.y``).  The author assembled the
parts against a common assumed-flat plane; authored space is the frame
in which they fit (partition document, section 3 step 1).

Exactness caveat (amendment A7): the anchor cancellation equates our
mesh sample at the anchor with X-Plane's render-time terrain sample.
Those agree only up to DSF elevation-pool quantisation — a per-object
constant of roughly centimetres.  Within one object the assembly is
exact regardless.  Do not chase a 2 cm cross-object "tear".
"""

from __future__ import annotations

Triangle = tuple[int, int, int]


def weld_parts(
    vertices: list[tuple[float, float, float]],
    triangles: list[Triangle],
) -> list[list[Triangle]]:
    """Split a triangle soup into position-welded connectivity classes
    ("parts") — level 1 of the part/structure/inherited hierarchy.

    Vertices at the same position (``VERTEX_WELD_DECIMALS``) are welded
    first: exporters duplicate a position once per texture seam or
    smoothing group, which would otherwise shatter a single wall into
    dozens of parts.  Callers pre-merge ``ANIM_begin`` blocks and fold
    ``ATTR_LOD`` copies before welding (partition document, section 3
    step 2); draped triangles never enter (invariant I-9).

    Subsumes the prototype's ``connected_components`` (amendment A10);
    ``group_components_into_structures`` is deliberately not ported.
    """
    raise NotImplementedError("workstream W2")


def contact_graph(
    vertices: list[tuple[float, float, float]],
    parts: list[list[Triangle]],
    epsilon_metres: float,
) -> set[tuple[int, int]]:
    """Return the set of part-index pairs whose surfaces come within
    ``epsilon_metres`` of each other.

    Broad phase: 3D axis-aligned bounding-box gap over a uniform grid
    (sound — box gap never exceeds surface gap).  Narrow phase:
    vertex-to-triangle surface distance in both directions with early
    exit at epsilon, pruning the broad-phase superset.  Any pair the
    narrow phase cannot prove apart (budget exhaustion, degenerate
    triangles, numerical doubt) KEEPS its edge (invariant I-20).

    Uses the 3D box, not the prototype's 2D box: the 2D box merges a
    jetbridge at y = 6 m with the shed beneath it.
    """
    raise NotImplementedError("workstream W2")


def connected_structures(
    part_count: int,
    contact_edges: set[tuple[int, int]],
) -> list[list[int]]:
    """Connected components of the contact graph: lists of part indices,
    one list per structure.  This IS the optimal partition (partition
    document, section 2.2) — there is no gap parameter to tune, and no
    post-hoc merging or splitting belongs here (large-ground-span
    handling is bake-and-flag in workstream W4, amendment A3, never a
    split)."""
    raise NotImplementedError("workstream W2")
