"""Hermetic unit tests for the clean-room single grade graph
(``auto_patch.grade_graph``).  No build/fixtures — pure geometry."""
import math
import pytest

from auto_patch import grade_graph as GG
from auto_patch.config import (
    APRON_MAX_GRADE, TAXI_MAX_GRADE, TAXI_MAX_GRADE_NARROW,
    SERVICE_ROAD_MAX_GRADE, TAXI_GRADE_BY_WIDTH,
)


def _square(side=20.0):
    """4-corner square apron/junction ring + keys."""
    ring = [(0.0, 0.0), (side, 0.0), (side, side), (0.0, side)]
    keys = [0, 1, 2, 3]
    return ring, keys


def _cap_of(sc, a, b):
    for (x, y, cap) in sc.edges:
        if {x, y} == {a, b}:
            return cap.flat_cap()
    return None


def test_apron_body_is_one_percent_no_spine():
    ring, keys = _square()
    s = GG.GradeShape(role="apron", ring=ring, keys=keys)
    ctx = GG.GradeContext(centerlines=[])
    sc = GG.shape_constraints(s, ctx)
    assert sc.edges, "apron must produce body edges"
    assert all(abs(cap.flat_cap() - APRON_MAX_GRADE) < 1e-9 for (_a, _b, cap) in sc.edges)


def test_junction_no_spine_inherits_cap():
    ring, keys = _square()
    s = GG.GradeShape(role="junction", ring=ring, keys=keys)
    # nearest connected taxiway is narrow (A/B) → 3%
    ctx = GG.GradeContext(
        centerlines=[], inherited_junction_cap=lambda sh: TAXI_MAX_GRADE_NARROW)
    sc = GG.shape_constraints(s, ctx)
    assert sc.edges
    assert all(abs(cap.flat_cap() - TAXI_MAX_GRADE_NARROW) < 1e-9
               for (_a, _b, cap) in sc.edges)


def test_junction_with_spine_uniform_taxiway_cap():
    # a centerline running through the middle of the square along x
    ring = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0),
            (0.0, 10.0), (20.0, 10.0)]   # last two ON the centerline y=10
    keys = list(range(len(ring)))
    cl = GG.Centerline(pts=[(0.0, 10.0), (20.0, 10.0)],
                       cap=TAXI_MAX_GRADE_NARROW)
    s = GG.GradeShape(role="junction", ring=ring, keys=keys)
    ctx = GG.GradeContext(centerlines=[cl])
    sc = GG.shape_constraints(s, ctx)
    # the two spine nodes (idx 4,5) share a centerline → spine cap (3%)
    assert _cap_of(sc, 4, 5) == pytest.approx(TAXI_MAX_GRADE_NARROW)
    # junction body is ALSO the taxiway cap → uniform
    assert all(abs(cap.flat_cap() - TAXI_MAX_GRADE_NARROW) < 1e-9
               for (_a, _b, cap) in sc.edges)
    # spine chain recorded, ordered along arc
    assert sc.spine_chains == [[4, 5]] or sc.spine_chains == [[5, 4]]


def test_apron_with_spine_taxi_on_spine_one_percent_body():
    ring = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0),
            (0.0, 10.0), (20.0, 10.0)]
    keys = list(range(len(ring)))
    cl = GG.Centerline(pts=[(0.0, 10.0), (20.0, 10.0)], cap=TAXI_MAX_GRADE)
    s = GG.GradeShape(role="apron", ring=ring, keys=keys)
    ctx = GG.GradeContext(centerlines=[cl])
    sc = GG.shape_constraints(s, ctx)
    # spine pair (4,5) at taxiway cap
    assert _cap_of(sc, 4, 5) == pytest.approx(TAXI_MAX_GRADE)
    # a body pair (corner 0 to corner 1) at apron 1%
    assert _cap_of(sc, 0, 1) == pytest.approx(APRON_MAX_GRADE)


def test_seam_endpoint_drops_pair():
    ring, keys = _square()
    s = GG.GradeShape(role="apron", ring=ring, keys=keys)
    ctx = GG.GradeContext(centerlines=[], seam_keys=frozenset({0}))
    sc = GG.shape_constraints(s, ctx)
    assert all(0 not in {a, b} for (a, b, _c) in sc.edges)


def test_service_junction_four_percent():
    ring, keys = _square()
    s = GG.GradeShape(role="service_junction", ring=ring, keys=keys)
    ctx = GG.GradeContext(centerlines=[])
    sc = GG.shape_constraints(s, ctx)
    assert all(abs(cap.flat_cap() - SERVICE_ROAD_MAX_GRADE) < 1e-9
               for (_a, _b, cap) in sc.edges)


def test_nonconvex_visibility_drops_chord_across_notch():
    # an L-shaped apron; the chord between the two far tips leaves the pavement
    ring = [(0, 0), (30, 0), (30, 10), (10, 10), (10, 30), (0, 30)]
    keys = list(range(len(ring)))
    s = GG.GradeShape(role="apron", ring=ring, keys=keys)
    ctx = GG.GradeContext(centerlines=[])
    sc = GG.shape_constraints(s, ctx)
    # (30,0) idx1 to (0,30) idx5: chord cuts across the missing quadrant
    assert _cap_of(sc, 1, 5) is None
