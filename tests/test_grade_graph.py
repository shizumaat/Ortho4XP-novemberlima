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
    """Seam-pin law (2026-07-03/04 seam architecture — ``grade_law.classify_pair``
    lines ~242-246 / ~314-315):

    * a pair with BOTH endpoints seam-pinned (an along-seam pair) is
      terrain-controlled → dropped from the law;
    * a pair with ONE seam endpoint STAYS in the law but never earns
      spine/blend credit — the seam pin is a graded-TO hard anchor, so the
      approach is clamped to the shape's own BODY cap;
    * RUNWAY-family shapes keep the full one-seam exemption (the FAA profile
      is solved separately; a mid-runway seam pin can contradict it locally).
    """
    # (1) BOTH-seam pair dropped; the one-seam pairs of the same shape stay.
    ring, keys = _square()
    s = GG.GradeShape(role="apron", ring=ring, keys=keys)
    ctx = GG.GradeContext(centerlines=[], seam_keys=frozenset({0, 1}))
    sc = GG.shape_constraints(s, ctx)
    assert _cap_of(sc, 0, 1) is None          # seam↔seam: terrain-controlled
    assert _cap_of(sc, 0, 3) is not None      # seam↔free: kept in the law

    # (2) ONE-seam pair clamped to the body cap: seam node 4 lies ON the taxi
    # centerline, so without the clamp the (4,5) spine pair would earn
    # TAXI_MAX_GRADE — the seam-pin approach grades at the apron body cap.
    ring2 = [(0.0, 0.0), (20.0, 0.0), (20.0, 80.0), (0.0, 80.0),
             (0.0, 40.0), (20.0, 40.0)]      # 4,5 ON the centerline y=40
    keys2 = list(range(len(ring2)))
    cl = GG.Centerline(pts=[(0.0, 40.0), (20.0, 40.0)],
                       seg_caps=[TAXI_MAX_GRADE])
    s2 = GG.GradeShape(role="apron", ring=ring2, keys=keys2)
    sc2 = GG.shape_constraints(
        s2, GG.GradeContext(centerlines=[cl], seam_keys=frozenset({4})))
    assert _cap_of(sc2, 4, 5) == pytest.approx(APRON_MAX_GRADE)

    # (3) runway-role one-seam exemption: every pair touching the seam pin
    # leaves the law entirely; the rest of the shape is still graded.
    s3 = GG.GradeShape(role="runway", ring=ring, keys=keys)
    sc3 = GG.plane_constraints(
        s3, GG.GradeContext(centerlines=[], seam_keys=frozenset({0})),
        cap=TAXI_MAX_GRADE)
    assert sc3.edges, "runway plane must still grade its seam-free pairs"
    assert all(0 not in (a, b) for (a, b, _c) in sc3.edges)


def test_service_junction_four_percent():
    ring, keys = _square()
    s = GG.GradeShape(role="service_junction", ring=ring, keys=keys)
    ctx = GG.GradeContext(centerlines=[])
    sc = GG.shape_constraints(s, ctx)
    assert all(abs(cap.flat_cap() - SERVICE_ROAD_MAX_GRADE) < 1e-9
               for (_a, _b, cap) in sc.edges)


def _deseg_runway_ring(length=800.0, width=40.0, stations=5):
    """A de-segmented single-poly runway ring (length ≫ width): profile
    stations as interior LONG-EDGE vertices, going up one edge and back the
    other.  Keys are ring indices; vertex ``k`` and ``2*stations-1-k`` share a
    station.  ``width²/length`` stays < the 5 m cluster tolerance so same-station
    cross-edge vertices cluster (as they do on a real runway)."""
    step = length / (stations - 1)
    left = [(i * step, 0.0) for i in range(stations)]
    right = [((stations - 1 - i) * step, width) for i in range(stations)]
    ring = left + right
    return ring, list(range(len(ring)))


def test_runway_station_clustering_and_predicate():
    """The station clusterer projects a de-seg ring's vertices onto the ref
    axis and groups them at 5 m; same-cross-end vertices share a station, and
    the domain predicate keeps only |Δstation| ≤ 1."""
    from auto_patch import grade_law as GL
    ring, _keys = _deseg_runway_ring(length=800.0, width=40.0, stations=5)
    stations = GL.runway_axis_station_indices(ring)
    # 5 stations, and the two long-edge vertices at each station share an index.
    assert stations == [0, 1, 2, 3, 4, 4, 3, 2, 1, 0]
    assert len(set(stations)) == 5
    # A LEGACY 4-corner square is NOT length ≫ width, so its longest pair is a
    # DIAGONAL that over-segments it — which is exactly why plane_constraints
    # gates the scoping on ``single_poly`` (this ring is never scoped).
    assert GL.runway_axis_station_indices(
        [(0.0, 0.0), (45.0, 0.0), (45.0, 45.0), (0.0, 45.0)]) == [0, 1, 2, 1]
    # Predicate: same / adjacent in domain, 2+ intervals out.
    assert GL.runway_within_pair_in_domain(2, 2)
    assert GL.runway_within_pair_in_domain(2, 3)
    assert not GL.runway_within_pair_in_domain(2, 4)


def test_plane_constraints_runway_single_poly_scopes_multistation():
    """USER RULING 2026-07-08 — a de-segmented runway RING's within-shape pair
    domain is scoped to LATERAL + same/adjacent-station; a pair spanning 2+
    station intervals leaves it (the FAA profile law owns the longitudinal
    grade).  The scoping is gated on ``single_poly`` so a LEGACY segmented rect
    is a byte-identical no-op."""
    ring, keys = _deseg_runway_ring(length=800.0, width=40.0, stations=5)
    ctx = GG.GradeContext(centerlines=[])
    n = len(ring)  # 10; vertex k and n-1-k share a station

    def _has(sc, a, b):
        return any({x, y} == {a, b} for (x, y, _c) in sc.edges)

    # single_poly=True → scoped.
    scoped = GG.plane_constraints(
        GG.GradeShape(role="runway", ring=ring, keys=keys, single_poly=True),
        ctx, cap=TAXI_MAX_GRADE)
    assert _has(scoped, 0, 9)      # same station (0,0) ↔ station 0 — LATERAL
    assert _has(scoped, 0, 1)      # adjacent station 0↔1
    assert _has(scoped, 1, 8)      # same station 1
    assert not _has(scoped, 0, 2)  # 2 intervals — longitudinal, dropped
    assert not _has(scoped, 0, 4)  # far — dropped
    assert not _has(scoped, 0, 5)  # opposite corner (station 0↔4) — dropped

    # single_poly=False (legacy segmented rect) → NO scoping, full all-pair.
    legacy = GG.plane_constraints(
        GG.GradeShape(role="runway", ring=ring, keys=keys, single_poly=False),
        ctx, cap=TAXI_MAX_GRADE)
    assert _has(legacy, 0, 2) and _has(legacy, 0, 4) and _has(legacy, 0, 5)
    assert len(legacy.edges) > len(scoped.edges)
    # Every legacy edge sits at the plane cap (the scoping only DROPS pairs,
    # never changes a surviving pair's budget).
    assert all(abs(cap.flat_cap() - TAXI_MAX_GRADE) < 1e-9
               for (_a, _b, cap) in scoped.edges)


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


def test_ds_decompose_never_inflates():
    """The decomposition is a ROTATION of the direct separation, never an
    inflation: ``Δs∥² + Δs⊥² == sep²`` exactly, including on a CURVED route.

    (The original arc-credit form — Δs∥ = along-route arc between the
    projections — was measured WRONG 2026-07-03: near curves two
    physically-close points earned budgets far beyond any surface cap, so
    12 %+ surface cliffs perpendicular to the spine were ruled legal.  The
    pavement between two nearby points is continuous; the standards regulate
    the SURFACE gradient, so the budget derives from the direct separation,
    only rotated into (∥, ⊥) for the cL/cT anisotropy.)"""
    # up 100 m then right 100 m: arc 200, chord (0,0)->(100,100) = 141.42
    route = GG.RouteChain(pts=[(0.0, 0.0), (0.0, 100.0), (100.0, 100.0)])
    sep = math.hypot(100.0, 100.0)
    dpar, dperp = GG.ds_decompose((0.0, 0.0), (100.0, 100.0), route)
    assert math.hypot(dpar, dperp) == pytest.approx(sep, abs=1e-6)
    assert dpar <= sep + 1e-9                           # never > direct
    # mid-leg pair: (0,40)->(0,90) is 50 m of pure along-route separation
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
