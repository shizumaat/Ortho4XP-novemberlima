"""Unit tests for the conformance insert-altitude rule, exercised with
synthetic geometry (no airport build / X-Plane install required).

Regression pins for the de-seg residual A2 fix (HECA 2026-07-08): the
final ``enforce_conformance`` weld used a crown-UNAWARE lerp of the host
edge's emitted altitudes, so inserting an already-solved vertex into a
neighbour's crown-discontinuous edge re-derived a value the solver never
produced (136.298 re-lerped to 136.415; the emit consensus averaged the
two claims to 136.36 — a 3.57 % within-shape pair beside runway 05R).

Covered (``conformance._make_insert_altitude`` resolution order):
  * COINCIDENT-ADOPT — a T-junction insert coinciding (canonical-point
    registry tolerance) with an already-emitted solved vertex adopts
    that vertex's altitude instead of the edge lerp.
  * WALL GUARD — a coincident donor farther in value than
    ``VERTEX_ALT_MERGE_TOL_M`` from the edge lerp (a deliberate
    wall/cliff, the emitter's node-split rule) is NOT adopted.
  * CROWN-AWARE INTERPOLATION — when the host edge's endpoints carry
    different crown drops and no donor value exists, the insert takes
    the z′-space interpolation minus its own drop (the transform
    ``crown.extend_field_to_new_ring_nodes`` uses).
  * BYTE-IDENTICAL FALLBACK — equal endpoint drops + no donor value
    yield exactly the historical plain lerp.
"""
from __future__ import annotations

from shapely.geometry import Polygon

from auto_patch.canonical_points import CanonicalPointRegistry
from auto_patch.conformance import enforce_conformance
from auto_patch.layout import (
    BuiltShape,
    PavementLayout,
    ROLE_JUNCTION,
    ROLE_RUNWAY,
    SHARED_VERTEX_TOL_M,
)


def _make_layout(*shapes: BuiltShape) -> PavementLayout:
    """Layout with a seeded canonical-point registry (production seeds
    every shape corner through ``get_or_add``; the tests mirror that)."""
    layout = PavementLayout(icao="TEST", anchor=(0.0, 0.0),
                            shapes=list(shapes))
    registry = CanonicalPointRegistry(tol_m=SHARED_VERTEX_TOL_M)
    for s in shapes:
        for (x, y) in list(s.polygon.exterior.coords):
            registry.get_or_add(float(x), float(y))
    layout.canonical_points = registry
    return layout


def _receiver_square(alt_left: float, alt_right: float) -> BuiltShape:
    """Receiver: a 20×20 junction whose BOTTOM edge runs (0,0)→(20,0)
    with altitudes ``alt_left`` → ``alt_right`` (the host edge every
    test inserts into, at its midpoint (10, 0), t = 0.5)."""
    ring = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    return BuiltShape(polygon=Polygon(ring), role=ROLE_JUNCTION,
                      node_altitudes=[alt_left, alt_right,
                                      alt_right, alt_left])


def _donor_triangle(apex_alt: float | None) -> BuiltShape:
    """Donor: a triangle BELOW the receiver touching it only at its apex
    (10, 0) — the classic T-junction (a vertex of one shape on the
    interior of another's edge).  ``apex_alt=None`` emits the shape with
    no altitude model at all (no donor VALUE, geometry only)."""
    ring = [(5.0, -10.0), (15.0, -10.0), (10.0, 0.0)]
    alts = None if apex_alt is None else [9.9, 9.9, float(apex_alt)]
    return BuiltShape(polygon=Polygon(ring), role=ROLE_JUNCTION,
                      node_altitudes=alts)


def _alt_at(shape: BuiltShape, x: float, y: float) -> float:
    ring = list(shape.polygon.exterior.coords)[:-1]
    alts = list(shape.node_altitudes)
    if len(alts) == len(ring) + 1:
        alts = alts[:-1]
    assert len(alts) == len(ring)
    for (vx, vy), a in zip(ring, alts):
        if abs(vx - x) < 1e-9 and abs(vy - y) < 1e-9:
            return a
    raise AssertionError(f"no vertex at ({x}, {y}) in {ring}")


def test_insert_adopts_coincident_solved_vertex():
    """A T-junction insert coinciding with an already-emitted solved
    vertex ADOPTS that vertex's altitude — the weld makes the two nodes
    ONE, so re-deriving a second value via the edge lerp only creates a
    disagreement the emit consensus then averages into a step."""
    receiver = _receiver_square(10.0, 10.4)      # lerp at midpoint: 10.2
    donor = _donor_triangle(apex_alt=10.05)      # solver's value there
    layout = _make_layout(receiver, donor)
    enforce_conformance(layout)
    assert _alt_at(receiver, 10.0, 0.0) == 10.05


def test_insert_adopts_through_registry_radius_drift():
    """Node identity is the registry's RADIUS rule (the same rule
    ``to_osm`` assigns OSM node ids by), not exact coordinates: ring
    reshaping leaves emitted vertices centimetres off their registered
    canonical point (HECA A2: 0.07 m), and the adopt must still fire."""
    receiver = _receiver_square(10.0, 10.4)      # lerp at midpoint: 10.2
    donor = _donor_triangle(apex_alt=10.05)
    layout = PavementLayout(icao="TEST", anchor=(0.0, 0.0),
                            shapes=[receiver, donor])
    registry = CanonicalPointRegistry(tol_m=SHARED_VERTEX_TOL_M)
    # Register a canonical point slightly OFF the shared vertex first,
    # so (10, 0) interns to it and is never its own exact entry.
    registry.get_or_add(10.03, 0.03)
    for s in (receiver, donor):
        for (x, y) in list(s.polygon.exterior.coords):
            registry.get_or_add(float(x), float(y))
    layout.canonical_points = registry
    assert registry.find_nearest(10.0, 0.0, registry.tol_m) != (10.0, 0.0)
    enforce_conformance(layout)
    assert _alt_at(receiver, 10.0, 0.0) == 10.05


def test_authority_receiver_never_adopts():
    """A RUNWAY ring is a value AUTHORITY (the FAA profile stamps it) —
    an insert into it keeps the edge's own interpolation, never a
    neighbour's claim (measured at KCLT: adopting into runway rings
    minted within 9 → 25 and runway_grade 0 → 5)."""
    receiver = _receiver_square(10.0, 10.4)
    receiver.role = ROLE_RUNWAY
    donor = _donor_triangle(apex_alt=10.05)
    layout = _make_layout(receiver, donor)
    enforce_conformance(layout)
    assert _alt_at(receiver, 10.0, 0.0) == 10.0 + 0.5 * (10.4 - 10.0)


def test_insert_never_adopts_across_a_wall():
    """A coincident donor farther in value than the emitter's node-split
    rule (``VERTEX_ALT_MERGE_TOL_M``) is a deliberate wall/cliff — the
    insert keeps the edge's own interpolation."""
    receiver = _receiver_square(10.0, 10.4)      # lerp at midpoint: 10.2
    donor = _donor_triangle(apex_alt=2.0)        # 8 m below: a wall
    layout = _make_layout(receiver, donor)
    enforce_conformance(layout)
    assert _alt_at(receiver, 10.0, 0.0) == 10.0 + 0.5 * (10.4 - 10.0)


def test_insert_interpolates_crown_aware_across_drop_discontinuity():
    """With no donor VALUE and endpoint crown drops that differ, the
    insert interpolates in uncrowned space z′ = z + c and subtracts its
    own drop — a plain z-space lerp across the discontinuity is the A2
    defect (it manufactures a within-shape grade step)."""
    receiver = _receiver_square(10.0, 10.4)
    donor = _donor_triangle(apex_alt=None)       # geometry-only donor
    layout = _make_layout(receiver, donor)
    registry = layout.canonical_points
    cp_left = registry.get_or_add(0.0, 0.0)
    cp_insert = registry.get_or_add(10.0, 0.0)
    layout._crown_drop_key = {cp_left: 0.30, cp_insert: 0.10}
    enforce_conformance(layout)
    # z′ lerp: (10.0 + 0.30) + 0.5 × ((10.4 + 0) − (10.0 + 0.30)) = 10.35
    # minus the insert's own drop 0.10 → 10.25.
    expected = (10.0 + 0.30) + 0.5 * ((10.4 + 0.0) - (10.0 + 0.30)) - 0.10
    assert abs(_alt_at(receiver, 10.0, 0.0) - expected) < 1e-9


def test_insert_plain_lerp_when_drops_equal_and_no_donor():
    """Equal endpoint drops + no coincident donor value = the historical
    plain lerp, expression-identical (byte-identical emit)."""
    receiver = _receiver_square(10.0, 10.4)
    donor = _donor_triangle(apex_alt=None)       # geometry-only donor
    layout = _make_layout(receiver, donor)
    enforce_conformance(layout)                  # no crown field at all
    assert _alt_at(receiver, 10.0, 0.0) == 10.0 + 0.5 * (10.4 - 10.0)
