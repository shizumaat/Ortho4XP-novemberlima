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
                       seg_caps=[TAXI_MAX_GRADE_NARROW])
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
    # Same topology as the junction spine test, scaled 4× in y so the bottom
    # body edge (corners 0→1) sits 40 m from the mid-height spine — beyond
    # APRON_TAXI_TRANSITION_M (30 m).  With O4_APRON_TAXI_BLEND on, a body edge
    # that is BOTH near AND along a running taxiway earns a blended cap (see
    # _apron_edge_cap); placing this edge past the transition lets it decay back
    # to the flat apron 1 % so the body-vs-spine distinction is what's tested.
    ring = [(0.0, 0.0), (20.0, 0.0), (20.0, 80.0), (0.0, 80.0),
            (0.0, 40.0), (20.0, 40.0)]
    keys = list(range(len(ring)))
    cl = GG.Centerline(pts=[(0.0, 40.0), (20.0, 40.0)], seg_caps=[TAXI_MAX_GRADE])
    s = GG.GradeShape(role="apron", ring=ring, keys=keys)
    ctx = GG.GradeContext(centerlines=[cl])
    sc = GG.shape_constraints(s, ctx)
    # spine pair (4,5) at taxiway cap
    assert _cap_of(sc, 4, 5) == pytest.approx(TAXI_MAX_GRADE)
    # a body pair (corner 0 to corner 1), 40 m from the spine → flat apron 1%
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


# ── ds_decompose: the anisotropic (Δs∥, Δs⊥) primitive (Phase 1) ──────────────

def test_ds_decompose_straight_route_is_isotropic():
    """A STRAIGHT route → (Δs∥, Δs⊥) == (sep, 0): straight taxiways/aprons see
    the legacy ``cap·dist`` budget, so wiring anisotropy can't change them."""
    route = GG.RouteChain(pts=[(0.0, 0.0), (100.0, 0.0)])
    # a pair strung ALONG the line
    dpar, dperp = GG.ds_decompose((10.0, 0.0), (40.0, 0.0), route)
    assert dpar == pytest.approx(30.0, abs=1e-6)
    assert dperp == pytest.approx(0.0, abs=1e-6)
    # a pair offset to the SAME side (parallel) — still zero transverse SEPARATION
    dpar2, dperp2 = GG.ds_decompose((10.0, 5.0), (40.0, 5.0), route)
    assert dpar2 == pytest.approx(30.0, abs=1e-6)
    assert dperp2 == pytest.approx(0.0, abs=1e-6)
    # a pure PERPENDICULAR pair → (0, perp)
    dpar3, dperp3 = GG.ds_decompose((20.0, 0.0), (20.0, 12.0), route)
    assert dpar3 == pytest.approx(0.0, abs=1e-6)
    assert dperp3 == pytest.approx(12.0, abs=1e-6)


def test_ds_decompose_curved_route_credits_arc():
    """A CURVED (L-bend) route → Δs∥ is the spine ARC (> chord), Δs⊥ ≈ 0 for two
    points ON the route.  This is the rising-curve fix: the climb is budgeted
    against the route's arc length, not its shorter chord."""
    # up 100 m then right 100 m: total arc 200, chord (0,0)->(100,100) = 141.42
    route = GG.RouteChain(pts=[(0.0, 0.0), (0.0, 100.0), (100.0, 100.0)])
    chord = math.hypot(100.0, 100.0)
    dpar, dperp = GG.ds_decompose((0.0, 0.0), (100.0, 100.0), route)
    assert dpar == pytest.approx(200.0, abs=1e-6)      # full arc
    assert dpar > chord + 50.0                          # arc >> chord
    assert dperp < 0.5                                  # both ON the route
    # mid-leg pair: (0,40)->(0,90) is 50 m of pure arc on the first leg
    dpar2, dperp2 = GG.ds_decompose((0.0, 40.0), (0.0, 90.0), route)
    assert dpar2 == pytest.approx(50.0, abs=1e-6)
    assert dperp2 == pytest.approx(0.0, abs=1e-6)


def test_ds_decompose_matches_centerline_and_routechain():
    """``ds_decompose`` is geometry-only and duck-typed: a single-segment route
    gives identical results whether passed a ``Centerline`` or a ``RouteChain``."""
    pts = [(0.0, 0.0), (50.0, 0.0)]
    rc = GG.RouteChain(pts=pts)
    cl = GG.Centerline(pts=pts, seg_caps=[TAXI_MAX_GRADE])
    a = GG.ds_decompose((5.0, 2.0), (35.0, 3.0), rc)
    b = GG.ds_decompose((5.0, 2.0), (35.0, 3.0), cl)
    assert a == pytest.approx(b, abs=1e-9)


# ── cT transverse-cap table (Phase 2) ────────────────────────────────────────

def test_taxi_transverse_cap_per_letter():
    """ICAO Annex 14 Table 3-2 transverse caps: A/B → 2 %, C–F → = longitudinal
    (isotropic).  When width-grading is off, cT collapses to cL everywhere."""
    from auto_patch.config import (
        taxi_transverse_cap_for_letter as cT,
        taxi_grade_cap_for_letter as cL,
        TAXI_MAX_TRANSVERSE_NARROW)
    # C–F (and unknown) are isotropic: cT == cL
    for L in ("C", "D", "E", "F", None, ""):
        assert cT(L, enabled=True) == cL(L, enabled=True)
    # A/B earn the 2 % transverse cap when width-grading is on
    assert cT("A", enabled=True) == pytest.approx(0.02)
    assert cT("B", enabled=True) == pytest.approx(TAXI_MAX_TRANSVERSE_NARROW)
    # cT (2 %) is BELOW cL (3 %) for A/B — anisotropic, not looser
    assert cT("A", enabled=True) < cL("A", enabled=True)
    # gate OFF → cT collapses to cL (isotropic) for every letter
    for L in ("A", "B", "C", "F"):
        assert cT(L, enabled=False) == cL(L, enabled=False)
