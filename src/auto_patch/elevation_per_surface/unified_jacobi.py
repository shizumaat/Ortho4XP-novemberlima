"""Per-surface unified Jacobi elevation solver (user 2026-05-03).

Lifted from ``auto_patch.elevation._solve_pavement_elevations_unified``
(commit ``35db401`` baseline) with three targeted changes that
implement the per-axis grade rule:

1. **Rects (taxi roles) get RING EDGES ONLY — no within-shape spatial
   pairs, plus a cross-section flatness constraint.**  Per user
   2026-05-03 terminology: a rect has two "sloping edges" (parallel
   to ``source_axis``, where slope is allowed at ≤ 1.5 % per metre
   of axial travel) and two "axis-end edges" (perpendicular to
   ``source_axis``, which must be EXACTLY FLAT — zero perpendicular
   delta).  Avoid "short" / "long" — a taxi rect can be wider than
   it is long (e.g. SPJC stub F).  The flatness is enforced as an
   equality constraint group on the two corners at each axis-end
   (``rect_flat_groups``), not a 1.5 %-cap edge.

   No diagonals.  No edges from a rect to a perpendicular runway
   150 m away.  Junction vertices touching a rect must coincide
   with the rect's corners only — no intermediate nodes on the
   sloping edge (handled upstream by junction emission rules).

2. **Junctions / aprons / terminals get RING + ALL-PAIR Euclidean
   spatial edges (no radius cap).**  These are multi-directional
   surfaces — a plane can taxi across in any direction, so the
   role's grade cap applies between any two vertices on the same
   polygon, not just ring-adjacent ones.  The legacy 60 m radius
   cap was the source of the F-stub-vs-runway grade violation
   (user 2026-05-03): a junction wider than 60 m had un-constrained
   vertex pairs that ended up at incompatible elevations.

3. **Terminals are SOFT, not HARD-anchored.**  Only runway corners
   (CIFP profile) are immutable.  Terminals enter the solver as
   soft nodes seeded from DEM-median, with a flatness constraint
   (all corners share one value, set to the iteration-average each
   pass).  The pre-solver "max grade-compliant from runway corners
   within 250 m" terminal pin is intentionally bypassed when this
   solver runs.

Cross-shape continuity uses shape-shared vertex buckets (same node
index in the unified graph), which makes elevation continuity at
shared corners automatic.
"""
from __future__ import annotations

import heapq
import math
import os as _os
import time as _time
from collections import deque

from shapely.errors import GEOSException, TopologicalError

from auto_patch.config import (
    APRON_CORRIDOR_GEODESIC, APRON_CORRIDOR_SEED_RADIUS_M,
    APRON_CORRIDOR_SMOOTH_GRADE, APRON_CORRIDOR_SMOOTH_RADIUS_M,
    NETWORK_PROFILE_MODEL, ROLE_GRADE_LIMITS, ROUTE_FIELD_LOCAL_WINDOW_M,
    ROUTE_FIELD_MODEL, ROUTE_NOISE_FRAC, RUNWAY_END_FRACTION,
    RUNWAY_END_GRADE, RUNWAY_MAX_GRADE, SURFACE_FAIRING,
    SURFACE_FAIRING_MAX_MOVE_M, TAXI_CORRIDOR_PROFILE,
    TAXIWAY_MAX_GRADE_CHANGE_PER_M, TERMINAL_LEAF_LEVELS,
    TERMINAL_PADS_SLOPE, WRITE_ARBITRATION)
from auto_patch.elevation import (
    APRON_MAX_GRADE, SERVICE_ROAD_MAX_GRADE, TAXI_MAX_GRADE)
from auto_patch.layout import (
    ROLE_APRON, ROLE_BOUNDARY, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_RUNWAY, ROLE_RUNWAY_CROSSING,
    ROLE_SECONDARY_PARALLEL, ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION,
    ROLE_STUB, ROLE_TERMINAL,
)

# Narrow exception tuple for shapely / numeric-geometry failure
# modes.  Programming errors propagate so they surface immediately.
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)


SLOPING_RECT_ROLES = (
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR,
    # Ground-vehicle service roads grade along their axis like a taxiway
    # (ring-only + flat cross-section), but at 4% — see _role_grade.
    ROLE_SERVICE_ROAD,
)

PAVEMENT_ROLES = {
    ROLE_RUNWAY, *SLOPING_RECT_ROLES,
    ROLE_APRON, ROLE_TERMINAL, ROLE_JUNCTION,
    # Service-road network junction: all-pair grading branch at 4%
    # (not a sloping rect — irregular fill polygon at bends/intersections).
    ROLE_SERVICE_JUNCTION,
    # Per user 2026-05-18: runway-crossing junctions carry runway-
    # interpolated ``node_altitudes`` from
    # ``_resolve_runway_crossings``.  Treat them as HARD-anchored
    # ring-only edges (same path as ``ROLE_RUNWAY``) so the solver
    # doesn't reshape elevations that the runway-interpolation
    # already established.
    ROLE_RUNWAY_CROSSING,
}

CAP_SWEEPS_PER_ITER = 5

# ── Priority cascade (user 2026-05-22) ───────────────────────────
# The solver runs as an ordered cascade rather than one simultaneous
# relaxation: seam + runway corners are the immutable HARD anchors,
# then each lower tier is solved against the FROZEN tier above it.
# Grade is sacred at every tier; the cascade order decides who yields
# to preserve it.
#
#   seam / runway  (HARD)
#     → TAXI network (rects + junctions): graded between the runway /
#       seam intersections it touches, closest to DEM within the
#       taxiway grade cap.  This is the "grade the taxi network like
#       the runway" step — its anchors are the shared runway/seam
#       nodes (already HARD-seeded); everything between follows terrain
#       clamped to grade, dipping below / rising above only as needed
#       to span the anchors.
#     → APRONS: the taxi network is now frozen; each apron adjusts (as
#       a whole, within apron grade) to meet its taxiways at the
#       shared boundary nodes.
#     → TERMINALS: aprons frozen; the flat terminal floor adjusts to
#       connect to its aprons within grade.
#
# Why phased (not simultaneous): a single relaxation is a tug-of-war —
# an apron held at DEM by its own attraction pins a taxiway it borders,
# forcing the taxiway over-grade (SPLP junction -10025).  Freezing the
# higher tier and letting the lower tier yield removes that conflict.
#
# Priority cascade INVERTED (user 2026-05-23): solve from the TERMINAL
# outward to the runway, not the runway inward.  Aircraft park at the
# terminal (which must stay flat) and must be able to taxi to every
# runway within grade, so the terminal is the anchor and the runway
# yields last.  Order of authority (solved first, frozen for the rest):
#   seam / runway-CIFP (HARD) > TERMINAL (flat, DEM-mean) > APRON > TAXI.
# A node's OWNER tier = the highest-priority role among the shapes that
# use it (TERMINAL > APRON > TAXI); a node shared by a terminal and an
# apron is terminal-owned, so the apron yields to the flat terminal floor
# (this is what keeps the terminal whole-flat with aprons matching 1:1 —
# no separate post-flatten needed).  A taxiway/apron node is apron-owned.
# Lower tiers couple to frozen higher tiers through these shared HARD
# nodes, NOT through cross-tier edges — so each phase uses only its own
# tier's edges.  (Runway is still base-HARD here; making it yield as the
# last resort is a separate step.)
_TIER_TERMINAL = 3
_TIER_APRON = 2
_TIER_TAXI = 1

_TAXI_TIER_ROLES = frozenset((*SLOPING_RECT_ROLES, ROLE_JUNCTION,
                              ROLE_SERVICE_JUNCTION))

# Per-axis junction grading (user 2026-05-22).  When True, junction grade
# constraints are LONGITUDINAL (along each converging centerline) + ring only;
# the unregulated inter-centerline DIAGONAL is dropped (the all-pair Euclidean
# cap is stricter than ICAO/EASA require and forbids junctions that
# legitimately slope along routes over real terrain, e.g. SPLP -10025).  Pairs
# with the audit (check_grade) which must also go per-axis or it will flag the
# diagonals this allows.  The audit goes per-axis whenever this flag is True
# (the grade test passes ``taxi_axes_ll`` gated on this flag), so they stay
# coupled.  ALSO drives the apron taxilane model (session 47): when True,
# aprons collect the apt.dat taxilane axes crossing them so along-lane pairs
# grade along the (looser) arc — directional relief inside aprons — while the
# general apron BODY (pairs off any lane) keeps its all-pair Euclidean cap.
# Per-axis junction grading (user 2026-06-10 ruling): the 1.5 % cap
# applies along the taxi CENTERLINE; a curved junction's cross-axis
# diagonal chords are an unregulated direction (ICAO Annex 14 §3.9 /
# EASA CS-ADR-DSN.D.265/.280 regulate longitudinal-along-route +
# transverse) and the inside-of-curve edge legitimately exceeds the cap
# for the centerline to carry it.  All-pair chords had pinned high-speed
# exit junctions flat (HECA #282/#283 could not rise toward A4/A5) and
# blocked smooth blends through turning junctions (#291: 75's axis
# bending into 95's).  check_grade mirrors via ``taxi_axes_ll``.
_PER_AXIS_JUNCTIONS = True

# Runway-flex third pass (user 2026-05-28).  The runway's elevation profile is
# DERIVED from the DEM (interpolated between CIFP threshold anchors); the DEM is
# the least-accurate part of the equation.  When a junction/stub cannot reach
# grade because it is wedged between a soft apron and a runway-anchored node
# that the DEM dipped/bulged (CYXY 14R/32L dips ~3 m to 691.4 around the 02/20
# intersection, forcing stub A to 8.9 %), the impossible connection has nowhere
# to go because EVERY runway node is HARD.  This pass keeps the real-world CIFP
# THRESHOLD endpoints hard but lets the runway's INTERIOR nodes flex within the
# runway grade cap, so the dip can rise toward the junction and the gap spreads
# over the runway's length instead of concentrating on the short connector.
# Only fires when a residual within-shape violation remains after the reverse
# pass (the impossible-connection signature) AND only commits if it strictly
# reduces the worst violation — so airports whose runway anchor is correct are
# untouched.  Makes the surface MORE faithful: CIFP thresholds are ground
# truth, pavement is known to exist and be gradeable, the DEM is the guess.
_RUNWAY_FLEX = True
_RUNWAY_FLEX_THRESHOLD_TOL_M = 2.0  # axial proximity to a runway END = threshold
# Seam-level threshold-yield: the band solve tilts the runway through the hard
# seam, which can leave a residual vertical-curve kink at the seam crossing
# (eliminating it needs the seam's runway node anchored in the FAA smooth — a
# follow-up).  Allow this many marginal kinks so a grade-compliant seam yield
# still commits instead of leaving the runway grade-violating.
_SEAM_CURV_KINK_ALLOWANCE = 2

# DEM attraction (user 2026-05-22): each iteration, pull every SOFT node a
# fixed fraction of the way toward its terrain (DEM) elevation, THEN
# cap-project.  This makes soft pavement settle "as close to DEM as the
# grade caps allow" — the documented intent ("reach the highs and lows in
# DEM that are possible within grade limits").  Without it,
# cap-projection-only never RAISES a node that warm-started low (a prior
# pass's value) back toward terrain, so a taxiway/apron network spanning
# low terminals and high runways sinks several metres below its own
# terrain (HECA T4 / taxiway T cliff: stub T4 sat 7 m below runway 05C/23C
# because the genuinely-low south terminals drained the connected network
# via cap-chains).
#
# The pull is PERSISTENT (no decay): the equilibrium balances the DEM
# spring against the per-edge caps, so each node ends as close to DEM as
# its caps permit and the low terminals no longer diffuse across the whole
# field.  Convergence is still clean — a node free to reach DEM converges
# geometrically (rate ``1 − DEM_ATTRACTION``); a cap-pinned node settles
# where the spring pull and the cap push-back cancel (net per-iter change
# → 0).  A DECAYING weight was tried first and FAILED: once it decayed the
# pure-cap tail relaxed the network back to the low-terminal compromise
# (HECA below-DEM unchanged).
DEM_ATTRACTION = 0.3
DEM_ATTRACTION_DECAY = 1.0
DEM_ATTRACTION_MIN = 1e-4

# Asymmetric DEM attraction (user 2026-05-22): grade > DEM, and a soft
# node must NOT be dragged BELOW its terrain unless grade toward a HARD
# anchor genuinely requires it.  A node sitting below its DEM was almost
# always pulled there by a cap-chain to a low connected shape (SPLP
# junction (4,503): every vertex 0.5-1.4 m below terrain, dragged by a
# taxiway descending to 64 m — even though the local terrain is flat ~72
# and a flat junction/stub would be grade-compliant).  So pull UP toward
# terrain STRONGLY (restore it); pull DOWN gently.  Grade still wins: the
# cap-projection runs AFTER this each iteration, so a node that truly
# must sit below DEM to stay within grade of a lower HARD anchor is
# pushed back down (the spring just sets the target, the cap has the last
# word).
DEM_FLOOR_ATTRACTION = 0.85


def _role_grade(role: str) -> float:
    """Per-role max grade cap — the SINGLE source of truth is
    ``config.ROLE_GRADE_LIMITS`` (taxiway-family + runway + junction 1.5 %;
    aprons 1.5 %; service roads 4 %; terminals = ``TERMINAL_MAX_GRADE``,
    0 = flat by default).  A ``None`` entry (boundary / retaining wall — no
    grade enforcement) maps to ``+inf`` so any pair passes; an unknown role
    falls back to the taxiway cap.  A cap of 0 is the FLAT signal (terminals
    by default) — the solver routes a 0-cap shape through the rigid flat-pad
    path instead of grading it."""
    cap = ROLE_GRADE_LIMITS.get(role, TAXI_MAX_GRADE)
    return float("inf") if cap is None else float(cap)


def _open_ring(coords) -> list[tuple[float, float]]:
    if coords and coords[0] == coords[-1]:
        return list(coords[:-1])
    return list(coords)


def solve(layout, icao: str,
          max_iters: int = 5000, tol_m: float = 0.001,
          dem=None, tile_lat: int = 0, tile_lon: int = 0) -> None:
    """Run the constrained-Laplacian solver and write elevations
    back onto each shape.  Mutates ``layout`` in place.

    ``dem`` is sampled per-vertex during seeding so SOFT nodes
    start at their natural terrain elevation; cap projection then
    pulls them toward HARD anchors only where the per-edge grade
    cap requires it.  Without DEM, soft nodes seed from existing
    layout values (or backfill from nearest HARD), which collapses
    DEM-elevated terrain to runway level.
    """
    t_start = _time.time()
    # O4_PERF=1: per-phase wall-clock breakdown (the build-perf
    # instrumentation — production tile builds print it per airport)
    _perf_on = _os.environ.get("O4_PERF") == "1"
    _perf: list = []
    _perf_t = [t_start]

    def _mark(label9):
        if _perf_on:
            now9 = _time.time()
            _perf.append((label9, now9 - _perf_t[0]))
            _perf_t[0] = now9

    nodes, bucket_to_idx = _build_node_list(layout)
    if not nodes:
        return
    n = len(nodes)

    elev, base_hard, _have_initial = _seed_elevations(
        layout, nodes, bucket_to_idx,
        dem=dem, tile_lat=tile_lat, tile_lon=tile_lon)
    if not any(base_hard):
        return

    # Per-node DEM elevation — the terrain target each tier is fit toward
    # (closest-to-DEM within grade).  Distinct from the seed (warm-start may
    # carry a stale value).
    dem_elev = _sample_node_dem(layout, nodes, dem, tile_lat, tile_lon)

    # Pre-solve terminal SEED from taxi-route grade feasibility (user 2026-06-09):
    # replace the too-high DEM seed (which the relief then ratchets further UP)
    # with the level each terminal can be while staying in grade to every adjacent
    # runway over its real taxiway route, so the apron can slope DOWN to the runway.
    _seed_terminals_from_taxi_routes(
        layout, elev, bucket_to_idx, dem_elev)

    _mark("seed")
    tiers = _node_tiers(layout, bucket_to_idx, n)

    # Per-tier edge sets.  Each phase grades a tier against the FROZEN tier
    # above it via shared HARD nodes, so a phase only needs its own tier's
    # edges — cross-tier coupling is carried by the shared node, not an edge.
    taxi_eg, taxi_el = _build_edges(
        layout, bucket_to_idx, roles=_TAXI_TIER_ROLES,
        add_runway_anchor=True)
    apron_eg, apron_el = _build_edges(
        layout, bucket_to_idx, roles=frozenset((ROLE_APRON,)),
        add_runway_anchor=False)
    term_eg, term_el = _build_edges(
        layout, bucket_to_idx, roles=frozenset((ROLE_TERMINAL,)),
        add_runway_anchor=False)

    total_iters = 0

    # (session 51, user 2026-05-28) Phase 1 = HOP-priority forward pass.
    # REPLACES the role-tier cascade (TERMINAL→APRON→TAXI), which inherited
    # altitudes via "yield to anchor" and could pull a taxi rect's DEM
    # 26.5-30.7m down to a flat 25.1m (Taxi L at SPJC).
    #
    # Algorithm (per docs/pipeline_invariants.md hierarchy):
    #   elevation_priority = hop distance from runway (junctions touching
    #     the runway = 1; each further-out shape +1).  Tie-break larger area
    #     first.
    #   Iterate by DESCENDING priority (leaves furthest from runway FIRST).
    #   Each shape: seed soft nodes at DEM, then `_project_shape` enforces
    #     the shape's own grade rule, HOLDING HARD + already-settled (leaf-
    #     ward) vertices.  Result: each shape sits DEM-close clamped to its
    #     OWN grade rule, instead of inheriting a far-away terminal's flat
    #     elevation.
    # If the forward pass reaches the runway grade-compliantly, we are done.
    # Otherwise Phase 2 relief (below) back-propagates outward.
    shape_constraints_p1 = _build_shape_constraints(layout, bucket_to_idx)
    runway_nodes = _runway_node_set(layout, bucket_to_idx)
    total_iters += _phase1_hop_priority(
        n, elev, base_hard, dem_elev, shape_constraints_p1, runway_nodes)
    _mark("phase1-cascade")

    # Relief — STEP 2 of the directional grade-relief algorithm (user
    # 2026-05-25).  STEP 1 is the cascade above (seed DEM, spread terminal ->
    # apron -> taxi -> runway, cap to grade); where it already reaches the
    # runway within grade, we are done and step 2 leaves it untouched.  Where
    # it does NOT, step 2 FLIPS the direction: it propagates grade compliance
    # OUTWARD from the runway/seam HARD anchors, building ON the cascade
    # (no reseed — we never throw the DEM-following surface away).  For each
    # over-grade edge it HOLDS the inward (runway-ward) node and moves ONLY the
    # outward node to the nearest compliant value, so the violation is pushed
    # OUT to the free terminal/apron end, which absorbs it — the compliant
    # interior is never disturbed.  Terminals translate as a RIGID flat unit
    # (their whole flat group shifts together); aprons/taxi flex per-node.
    #
    # This replaces the old SYMMETRIC relief, which reseeded to DEM and
    # relaxed every edge both ways — converging to the LOWEST feasible surface
    # and over-dropping compliant nodes (e.g. SPLP junction 21 → 70.7, ~1 m
    # below its feasible band, breaking stub A that the cascade had solved).
    #
    # STEP 3 (shift runway thresholds + re-profile) stays the last resort, for
    # a genuine multi-runway squeeze where no outward move can satisfy all
    # runway connections (SPJC) — see pipeline / runway_redistribute.
    relief_hard = [base_hard[i] or tiers[i] == 0 for i in range(n)]
    # Built UNCONDITIONALLY: the post-relief passes (final co-level
    # reconcile at function level, writeback-adjacent users) need the
    # constraint set even when EVERY node is relief-hard and the whole
    # relief block below is skipped — an airport with only runway/seam
    # nodes (no soft taxi/apron pavement) crashed with an
    # UnboundLocalError here (user production tile, 2026-06-12).
    shape_constraints = _build_shape_constraints(
        layout, bucket_to_idx)
    if not all(relief_hard):
        relief_eg = dict(taxi_eg)
        relief_eg.update(apron_eg)
        relief_eg.update(term_eg)
        relief_el = dict(taxi_el)
        relief_el.update(apron_el)
        relief_el.update(term_el)

        _step_dbg = _os.environ.get("O4_STEP_DEBUG") == "1"
        _trace_n = [int(t) for t in
                    _os.environ.get("O4_TRACE_NODES", "").split(",")
                    if t.strip().isdigit()]

        def _trace(tag):
            if _trace_n:
                print("[trace]", tag, " ".join(
                    f"n{t}={elev[t]:.2f}" for t in _trace_n if t < n))
        _trace("pre-STEP1")
        if _step_dbg:
            v, w = _count_within_viol(elev, shape_constraints)
            print(f"[step] {icao} after STEP1 forward cascade: "
                  f"within-edge viol={v} worst={w:.2f}m")
        _trace("post-STEP1")
        total_iters += _directional_relief(
            n, elev, relief_hard, relief_eg, relief_el,
            shape_constraints, _RELIEF_MAX_ITERS, tol_m)
        _mark("relief-1")
        if _step_dbg:
            v, w = _count_within_viol(elev, shape_constraints)
            print(f"[step] {icao} after STEP2 reverse relief+yield: "
                  f"within-edge viol={v} worst={w:.2f}m")
        _trace("post-STEP2")
        # STEP 3 (user 2026-05-28): an impossible apron<->runway connection
        # (a junction/stub wedged between soft apron and a DEM-dipped
        # runway-anchored node) has nowhere to go while every runway node
        # is HARD.  Free the runway INTERIOR (CIFP thresholds stay hard)
        # and re-solve the bands so the dip rises and the gap spreads over
        # the runway's length.  No-op unless a residual violation remains.
        total_iters += _relax_runway_and_resolve(
            n, elev, layout, bucket_to_idx, base_hard,
            shape_constraints, tol_m,
            relief_eg=relief_eg, relief_el=relief_el)
        _mark("runway-flex-step3")
        # FINAL within-shape enforcement (difference-constraint solve): drive
        # every FEASIBLE within-shape edge to <=cap against the now-settled
        # runway/seam anchors, and report the band-pinned residual (the only
        # part a within-shape solve cannot fix — it needs an anchor to flex).
        if _step_dbg:
            v, w = _count_within_viol(elev, shape_constraints)
            print(f"[step] {icao} after STEP3 runway flex: "
                  f"within-edge viol={v} worst={w:.2f}m")
        _trace("post-STEP3")
        # TAXI-CORRIDOR PROFILES (user 2026-06-10): each chain of taxi rects
        # continuing through junctions becomes one smooth grade+curve-capped
        # 1-D profile (runway-centerline treatment, DEM lowest priority);
        # the written corridor nodes are HELD through the final enforcement
        # so the surrounding pavement conforms to the corridor, not the
        # reverse (taxi routes outrank aprons in the user's priority model).
        corridor_held: set = set()
        corridor_exempt: set = set()
        if TAXI_CORRIDOR_PROFILE:
            snap_c = list(elev)
            coupling_c = _build_level_coupling(shape_constraints)
            corridor_held, corridor_exempt, rwy_dem = \
                _taxi_corridor_profiles(
                    layout, elev, bucket_to_idx, base_hard, nodes=nodes,
                    coupling=coupling_c,
                    dem_ctx=(dem, tile_lat, tile_lon))
            _mark("corridor-pass-1")
            # CORRIDOR → RUNWAY FLEX FEEDBACK (user 2026-06-10: "once
            # pavement reaches max grade the runway flexes a bit"):
            # freeze-skipped ties blocked at/through a runway contact are
            # a measured demand — restore the pre-corridor surface,
            # re-smooth the runway through the demanded bounds (the
            # bounded re-smooth's envelope + consistency filters guard
            # the thresholds), re-grade the pavement against the flexed
            # runway (the corridor anchors live on the ADJACENT junctions
            # — without the relief they re-measure the stale values), and
            # re-run the corridor pass.  ONE round only: the demand is
            # honest only against the ORIGINAL saturated state (the s68
            # snapshot0 lesson) — re-measuring after the relief re-levels
            # the network toward DEM chases the dip circularly (measured:
            # 107.9 → 106.3 → asks 105.5 — the (a) over-dip class).
            # Under WRITE_ARBITRATION, TWO rounds: the transitive tie
            # demands (s77p2) only become measurable after the first
            # flex re-ties the network, and their basis is HARD-anchored
            # (HECA #256: the G side is pinned by the 60.65 contact, so
            # the demand converges instead of chasing — user-predicted
            # 23C ≈ 108; round-2 measured 108.46).  The merge below
            # keeps deepening monotone and round-3 demands are still
            # DISCARDED, so the worst case is one bounded extra dip.
            for _fb in range(2 if WRITE_ARBITRATION else 1):
                if not (rwy_dem[0] or rwy_dem[1]):
                    break
                dem_lo_c, dem_hi_c, dem_refs_c = rwy_dem
                elev[:] = snap_c
                _resmooth_runways_in_elev(
                    layout, elev, bucket_to_idx,
                    _runway_threshold_nodes(layout, bucket_to_idx)
                    | _seam_pinned_runway_nodes(layout, bucket_to_idx)
                    | _runway_crossing_nodes(layout, bucket_to_idx),
                    demand_lo=dem_lo_c, demand_hi=dem_hi_c,
                    only_refs=dem_refs_c)
                hard_rw = list(base_hard)
                for i in runway_nodes:
                    if i < n:
                        hard_rw[i] = True
                total_iters += _directional_relief(
                    n, elev, hard_rw, relief_eg, relief_el,
                    shape_constraints, _RELIEF_MAX_ITERS, tol_m)
                _mark("flex-relief")
                snap_c = list(elev)
                corridor_held, corridor_exempt, rwy_dem2 = \
                    _taxi_corridor_profiles(
                        layout, elev, bucket_to_idx, base_hard,
                        nodes=nodes, coupling=coupling_c,
                        dem_ctx=(dem, tile_lat, tile_lon))
                _mark("corridor-pass-flex")
                # merge: keep the deeper of the committed and re-measured
                # demands so round 2 never un-dips round 1
                nxt_hi = dict(dem_hi_c)
                for i2, v2 in rwy_dem2[1].items():
                    nxt_hi[i2] = min(nxt_hi.get(i2, float("inf")), v2)
                rwy_dem = (rwy_dem2[0],
                           nxt_hi if rwy_dem2[1] else {},
                           rwy_dem2[2] | dem_refs_c)
            if corridor_held:
                # FULL relief re-run against the committed corridors (the
                # same lesson as the runway flex: corridors move metres;
                # the enforce's band projection alone cannot redistribute
                # that through the network — the outward shape-cascade
                # can).  Runways + corridors are the hard set.
                hard_c = list(base_hard)
                for i in runway_nodes:
                    if i < n:
                        hard_c[i] = True
                for i in corridor_held:
                    hard_c[i] = True
                total_iters += _directional_relief(
                    n, elev, hard_c, relief_eg, relief_el,
                    shape_constraints, _RELIEF_MAX_ITERS, tol_m)
                _mark("relief-post-corridor")
            if _step_dbg:
                v, w = _count_within_viol(elev, shape_constraints)
                print(f"[step] {icao} after corridor profiles+relief: "
                      f"within-edge viol={v} worst={w:.2f}m")
        _enforce_within_shape_grade(
            elev, shape_constraints, base_hard,
            nodes=nodes, layout=layout, bucket_to_idx=bucket_to_idx,
            icao=icao, held_extra=corridor_held,
            band_exempt=corridor_exempt)
        _mark("enforce")
        if _os.environ.get("O4_TRACE_LL"):
            for part9 in _os.environ["O4_TRACE_LL"].split(";"):
                try:
                    la9, lo9 = (float(x) for x in part9.split(","))
                    xq, yq = layout.ll_to_m(la9, lo9)
                except _GEOM_EXC:
                    continue
                for i9, (xn9, yn9) in enumerate(nodes):
                    if (xn9 - xq) ** 2 + (yn9 - yq) ** 2 <= 2.25:
                        print(f"[trace] post-enforce n{i9}="
                              f"{elev[i9]:.2f} "
                              f"held={i9 in (corridor_held or ())}")
        owners: dict = {}
        for sc in shape_constraints:
            for i in sc["nodes"]:
                owners[i] = owners.get(i, 0) + 1
        # SLOPING-PAD POLISH (TERMINAL_PADS_SLOPE, s73): the global POCS
        # plateaus before converging terminal INTERIORS (s67 measured:
        # terminal4 carries 12 % lumps globally yet grades to 0 violations
        # in 25 ISOLATED cap-projection sweeps).  Per-pad isolated polish:
        # hold every node the pad shares with another shape (+ hard
        # anchors) so seams cannot move, cap-project the pad's private
        # nodes on its own visibility edges.  Seam-preserving by
        # construction — cross/v2e/mid metrics untouched.
        if TERMINAL_PADS_SLOPE:
            for sc in shape_constraints:
                if sc["role"] != ROLE_TERMINAL or not sc["edges"]:
                    continue
                held_t = {i for i in sc["nodes"]
                          if base_hard[i] or owners.get(i, 0) > 1}
                if len(held_t) == len(sc["nodes"]):
                    continue
                _project_shape(elev, sc["nodes"], held_t, sc["edges"],
                               False)
        # EDGE-PLANE SNAP (s73): a junction vertex the geometry passes
        # left a designed standoff from a sloping rect's or RUNWAY's long
        # edge (the 1.0 m vertex push; mouth drift) must still carry the
        # rect's edge-plane altitude — the surfaces meet across the gap.
        # SPJC's 0.61 m grade-gate step is a 4-vertex junction holding
        # 15.3 one metre off a runway long edge whose plane reads 15.9.
        # The first build of this pass missed it by EXCLUDING runway
        # edges.  Snap, then locally re-project each touched junction
        # (snapped + shared + hard held) so the correction grades through
        # the body.  (HECA's mid-edge graze class is handled by the
        # pre-solve corner insertion in pavement/vertices.py.)
        _snap_junction_verts_to_rect_edge_plane(
            layout, elev, bucket_to_idx, shape_constraints, base_hard,
            owners)
        _mark("polish+snap")

    # FINAL CO-LEVEL RECONCILE: post-enforce passes that move a single
    # vertex (edge-plane snap, twist, polish) can decohere a rect
    # flat-end coupled pair; the plane emit then averages the pair and
    # disagrees with the junction ring at BOTH corners (SPJC V3 ↔ #97:
    # solve left 15.8/16.1, emit wrote 15.9 = a 0.2 m cross step at
    # d=0).  Re-level every group to one value before writeback.
    n_lvl = _reconcile_level_coupling(elev, shape_constraints, base_hard)
    if n_lvl and _os.environ.get("O4_STEP_DEBUG") == "1":
        print(f"  [step] re-levelled {n_lvl} decohered flat-end group(s)")

    _mark("reconcile")
    n_terms, n_rects, n_juncs = _writeback(
        layout, elev, bucket_to_idx)
    if NETWORK_PROFILE_MODEL:
        _retreat_route_pinned_apron_edges(
            layout, dem_ctx=(dem, tile_lat, tile_lon),
            bucket_to_idx=bucket_to_idx)
    _mark("writeback")
    if _perf_on:
        tot9 = _time.time() - t_start
        parts9 = " ".join(f"{k}={v:.1f}s" for (k, v) in _perf
                           if v >= 0.05)
        print(f"  [perf] {icao} solve {tot9:.1f}s: {parts9}")
    _report(icao, total_iters, max_iters,
             _time.time() - t_start,
             n_terms, n_rects, n_juncs)


_SPREAD_COMPLY_TOL_M = 0.02  # iterate until every edge is within this of cap
# Relief iteration budget — umbrella ceiling for the within-bands convergence.
_RELIEF_MAX_ITERS = 12000


def _project_shape(elev, nodes, held, edges, flat, coupling=None) -> None:
    """Make ONE shape internally grade-compliant, holding ``held`` (node idxs
    already settled by inward shapes / HARD anchors); free nodes move the
    minimum needed.  This is the per-shape realisation of "a node_altitudes
    polygon stays compliant as a whole": it may slope up to its cap, but the
    moment a pair would exceed the cap the rest of the shape is dragged along.

    ``flat`` (terminal): every free node takes the held level (or the shape
    mean if nothing is held) — the whole plane translates as a rigid unit.
    Otherwise: a cap projection on the shape's OWN edges (rect = flat-cross +
    axial; apron/junction = all-pair), so the surface flexes but never shears.

    ``coupling`` (user 2026-05-28): an optional ``node -> (members...)`` map of
    RIGID LEVEL GROUPS — node sets that must share one elevation (a rect's two
    flat-end cross-corner pairs; a terminal's nodes).  When the projection moves
    any node, the WHOLE coupled group moves with it, so a rect's flat end stays
    flat no matter which neighbour drags it.  A group is "held" iff ANY member
    is held (an inward neighbour pinned one corner -> the level is pinned).
    """
    free = [i for i in nodes if i not in held]
    if not free:
        return
    if flat:
        src = [i for i in nodes if i in held]
        lvl = (sum(elev[i] for i in src) / len(src) if src
               else sum(elev[i] for i in nodes) / len(nodes))
        for i in free:
            elev[i] = lvl
        return

    # Precompute members + held flags for every edge endpoint ONCE; both
    # are invariant across the 400-sweep loop (same hoist as
    # _project_within_bands).  Numerically identical, just no per-edge
    # ``_members``/``any`` recomputation each sweep.
    memd: dict = {}
    heldd: dict = {}
    for i in nodes:
        grp = coupling[i] if (coupling is not None and i in coupling) else (i,)
        memd[i] = grp
        heldd[i] = any(m in held for m in grp)

    def _move(i, d):
        for m in memd[i]:
            elev[m] += d

    for _ in range(400):
        mx = 0.0
        for (i, j, cap) in edges:
            d = elev[i] - elev[j]
            ex = abs(d) - cap
            if ex <= 0.0:
                continue
            if ex > mx:
                mx = ex
            s = 1.0 if d > 0 else -1.0
            hi = heldd[i]
            hj = heldd[j]
            if hi and hj:
                continue
            if hi:
                _move(j, s * ex)
            elif hj:
                _move(i, -s * ex)
            else:
                _move(i, -0.5 * s * ex)
                _move(j, 0.5 * s * ex)
        if mx < 0.01:
            break


def _project_within_bands(elev, edges, is_hard, lo, hi, coupling,
                          held_extra, max_sweeps, tol) -> tuple[int, float]:
    """Cap-project all within-shape grade edges to convergence, CLAMPED to the
    per-node feasible bands ``[lo, hi]`` (user 2026-05-28).

    The directional two-pass leaves residual at cycle-closing edges; relaxing
    all edges fixes them, but unbounded relaxation on the dense all-pair apron
    edges was slow / SOR-divergent.  Clamping every node back into its
    difference-constraint band each sweep keeps it ANCHOR-feasible and bounded,
    so the iteration can't run away and converges.  HARD anchors + ``held_extra``
    (terminals, kept flat by the reverse pass) never move; both-held edges are
    skipped (infeasible seam cross-slopes — terrain-dictated).  Rigid groups
    (rect flat ends) move and clamp TOGETHER via ``coupling`` so flat ends stay
    flat.  Returns (sweeps, max_fixable_viol).
    """
    held_extra = held_extra or set()
    INF = float("inf")

    # Precompute per-node members + held flags ONCE.  Both are invariant
    # across the sweep loop (coupling / is_hard / held_extra never change),
    # but the old closures recomputed them for every edge endpoint on every
    # sweep — millions of dict lookups + ``any()`` calls.  Hoisting them out
    # is numerically identical (same sequential cap-projection order) and is
    # the bulk of this function's cost on apron-heavy airports.
    n_nodes = len(elev)
    mem: list = [None] * n_nodes
    held: list = [False] * n_nodes
    for i in range(n_nodes):
        grp = coupling[i] if (coupling is not None and i in coupling) else (i,)
        mem[i] = grp
        held[i] = any(is_hard[m] or m in held_extra for m in grp)

    def _move(i, d):
        for m in mem[i]:
            elev[m] += d

    # ONE-TIME clamp: seed every soft node into its anchor-feasible band — this
    # is the difference-constraint surface, the bulk of the correction (e.g.
    # CYXY 7.9 m -> 1.2 m).  Move a coupled group together to the intersection
    # of its members' bands.  INFEASIBLE nodes (lo>hi: the terrain-dictated seam
    # cross-slope) are left at their directional value — clamping them to a
    # midpoint each sweep destabilised the relaxation.
    seen_g: set = set()
    for i in range(n_nodes):
        if held[i]:
            continue
        grp = mem[i]
        if len(grp) > 1:
            key = tuple(sorted(grp))
            if key in seen_g:
                continue
            seen_g.add(key)
            glo = max((lo[m] for m in grp), default=-INF)
            ghi = min((hi[m] for m in grp), default=INF)
        else:
            glo, ghi = lo[i], hi[i]
        if glo == -INF and ghi == INF:
            continue
        if glo > ghi:
            continue                       # infeasible — leave as is
        v = min(max(elev[i], glo), ghi)
        for m in grp:
            elev[m] = v

    # Plain cap projection from that seed (terminals held, both-HARD seam edges
    # skipped) — bounded by the good start; no per-move re-clamp.
    mx = 0.0
    sweep = 0
    for sweep in range(max_sweeps):
        mx = 0.0
        for (i, j, cap) in edges:
            d = elev[i] - elev[j]
            ex = abs(d) - cap
            if ex <= 0.0:
                continue
            hi_i = held[i]
            hj = held[j]
            if hi_i and hj:
                continue            # both immovable: infeasible seam edge, skip
            if ex > mx:
                mx = ex
            s = 1.0 if d > 0 else -1.0
            if hi_i:
                _move(j, s * ex)
            elif hj:
                _move(i, -s * ex)
            else:
                _move(i, -0.5 * s * ex)
                _move(j, 0.5 * s * ex)
        if mx < tol:
            break
    return sweep + 1, mx


def _corridor_segments(layout, split: bool = False,
                       include_roads: bool = True):
    """Taxi-corridor polyline segments: apt.dat/OSM taxi centerlines PLUS
    every taxi rect's ``source_axis`` (discovered taxiways carry no apt.dat
    row; CYXY's TX1 apron is served only by discovered rects).
    ``split=True`` returns ``(apt_segs, axis_segs)`` — the network-profile
    graph needs the provenance (apt rows are the route-graph plain set;
    axis nodes enter it across straight gaps, the law's anchor-entry
    mechanic).  ``include_roads=False`` drops the ground-vehicle SVC
    centerlines (s79 Step D): an APRON must never bind to a road's
    profile — the road descends at 4 % toward terrain and is
    wall-separated; corridor-seeding aprons from it split the apron
    into two write families (HECA #266: 98 vs 102.7, 30 violations).
    Rect source AXES already exclude ROLE_SERVICE_ROAD."""
    apt_segs: list = []
    for entry in (getattr(layout, "apt_taxi_centerlines", None) or []):
        ls = entry[0] if isinstance(entry, (tuple, list)) else entry
        if (not include_roads and isinstance(entry, (tuple, list))
                and len(entry) > 1 and str(entry[1]).startswith("SVC")):
            continue
        try:
            cs = list(ls.coords)
        except (AttributeError, TypeError):
            continue
        apt_segs.extend(zip(cs, cs[1:]))
    axis_segs: list = []
    for s in layout.shapes:
        if s.role not in SLOPING_RECT_ROLES or s.role == ROLE_SERVICE_ROAD:
            continue
        ax = getattr(s, "source_axis", None)
        if ax is None or ax.is_empty:
            continue
        try:
            cs = list(ax.coords)
        except (AttributeError, TypeError):
            continue
        axis_segs.extend(zip(cs, cs[1:]))
    if split:
        return apt_segs, axis_segs
    return apt_segs + axis_segs


def _seg_grid(segs, cell):
    """Coarse spatial index over polyline segments (query = 3×3 cells)."""
    grid: dict = {}
    for k, ((ax, ay), (bx, by)) in enumerate(segs):
        for gx in range(int(min(ax, bx) // cell),
                        int(max(ax, bx) // cell) + 1):
            for gy in range(int(min(ay, by) // cell),
                            int(max(ay, by) // cell) + 1):
                grid.setdefault((gx, gy), []).append(k)
    return grid


def _corridor_point_nearest(x, y, segs, grid, cell):
    """Nearest corridor point over the 3×3 grid neighbourhood: returns
    ``(distance, px, py)`` (exact for distances ≤ cell; beyond that
    ``(+inf, x, y)``)."""
    gx0, gy0 = int(x // cell), int(y // cell)
    best = float("inf")
    bx0, by0 = x, y
    for dgx in (-1, 0, 1):
        for dgy in (-1, 0, 1):
            for k in grid.get((gx0 + dgx, gy0 + dgy), ()):
                (ax, ay), (bx, by) = segs[k]
                dx, dy = bx - ax, by - ay
                s2 = dx * dx + dy * dy
                if s2 < 1e-12:
                    continue
                t = ((x - ax) * dx + (y - ay) * dy) / s2
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                px, py = ax + t * dx, ay + t * dy
                d = math.hypot(x - px, y - py)
                if d < best:
                    best, bx0, by0 = d, px, py
    return best, bx0, by0


def _corridor_point_distance(x, y, segs, grid, cell):
    """Min point→segment distance (see ``_corridor_point_nearest``)."""
    return _corridor_point_nearest(x, y, segs, grid, cell)[0]


def _apron_zone_scaled_edges(shape_constraints, in_zone) -> list:
    """Apron grade edges whose BOTH endpoints satisfy ``in_zone``, caps
    scaled from the apron legal grade down to the smoothing grade."""
    out: list[tuple[int, int, float]] = []
    for sc in shape_constraints:
        if sc["role"] != ROLE_APRON:
            continue
        role_cap = _role_grade(ROLE_APRON)
        if role_cap <= 0:
            continue
        scale = APRON_CORRIDOR_SMOOTH_GRADE / role_cap
        if scale >= 1.0:
            continue
        for (i, j, c) in sc["edges"]:
            if c <= 0.0:
                continue
            if in_zone(i) and in_zone(j):
                out.append((i, j, c * scale))
    return out


def _apron_corridor_zone_edges(layout, nodes, shape_constraints):
    """APRON CORRIDOR SMOOTHING edge set (s76, user in-sim verdict at CYXY:
    aprons read much too steep at the 1.5 % legal cap even where taxi routes
    look right).  Aprons should fall away from the taxi corridors that serve
    them at ideally ``APRON_CORRIDOR_SMOOTH_GRADE`` (1 % — also the ICAO
    Annex 14 apron recommendation) within
    ``APRON_CORRIDOR_SMOOTH_RADIUS_M`` (200 m).

    STRAIGHT-LINE zone test — the ``APRON_CORRIDOR_GEODESIC`` gate-off
    path (s77 measured: this misattributes across grass; the geodesic
    state below supersedes it).  The caller projects the returned edges
    BEST-EFFORT after the legal enforce: a solver preference, NOT a law
    change (the validator still asserts ROLE_GRADE_LIMITS); wherever hard
    anchors genuinely demand more than 1 %, the projection plateaus and
    the legal surface stands."""
    if (APRON_CORRIDOR_SMOOTH_GRADE <= 0.0
            or APRON_CORRIDOR_SMOOTH_RADIUS_M <= 0.0):
        return []
    segs = _corridor_segments(layout, include_roads=False)
    if not segs:
        return []
    R = APRON_CORRIDOR_SMOOTH_RADIUS_M
    grid = _seg_grid(segs, R)
    zone_cache: dict[int, bool] = {}

    def _in_zone(i):
        hit = zone_cache.get(i)
        if hit is None:
            x, y = nodes[i]
            hit = _corridor_point_distance(x, y, segs, grid, R) <= R
            zone_cache[i] = hit
        return hit

    return _apron_zone_scaled_edges(shape_constraints, _in_zone)


# Taxi-rect vertices always seed the geodesic corridor field (the rect IS
# the corridor surface); their transverse offset to the axis is bounded by
# the half-width — beyond this something is wrong, don't seed.
_RECT_SEED_MAX_D0_M = 60.0

# Max move for the write-arbitration soft-terminus projection (the p10d
# J-tail lesson: an unbounded terminus move manufactures walls elsewhere).
_TERM_PROJ_MAX_M = 2.5

# Max move for the terminal LEAF re-level toward the adjacent-apron median
# (bounded so one badly-pinned apron region cannot relocate a whole pad).
_LEAF_MAX_MOVE_M = 3.0


def _apron_corridor_geodesic_state(layout, nodes, elev, shape_constraints,
                                   all_edges):
    """GEODESIC corridor zone + corridor-VALUE bands (s77, user-approved
    upgrade of the straight-line zone above — both improvements together):

    1. ATTRIBUTION — zone membership by the shortest INTERIOR path through
       pavement, not straight-line distance (s77 measured at HECA: apron
       vertices 13-65 m across grass from a centerline whose true interior
       path is 0.7-1.6 km — the Euclidean zone smooths them against a
       corridor that does not serve them).
    2. VALUE BINDING — in-zone apron vertices are clamped (best-effort)
       into bands [corridor_value ± g·interior_distance]: internal
       pair-cap scaling alone cannot see an apron sitting on a uniform
       OFFSET (wall) from the corridor that serves it.

    Mechanism: corridor-adjacent vertices (within
    ``APRON_CORRIDOR_SEED_RADIUS_M`` of a corridor polyline, plus every
    aircraft taxi-rect vertex) SEED the field at their own solved values
    with the transverse allowance ``± g·d0``; one multi-source Dijkstra
    over the solver's edge graph at plain lengths yields the interior
    distance (zone membership ≤ ``APRON_CORRIDOR_SMOOTH_RADIUS_M``), two
    more at ``g``-scaled lengths yield the value bands (the
    ``_lipschitz_tighten_bands`` relaxation seeded at corridor values).

    s77 measured calibration: at the 1.5 % legal cap this field already
    holds everywhere (the surface is assembled from cap-bounded edges) —
    the lever is the 1 % preference; route-law-pinned vertices (HECA's
    #186-squeeze family) must YIELD, so the caller intersects these bands
    with the legal route bands and keeps the legal ones wherever the
    intersection is empty.

    Returns ``(geo_dist, lo, hi)`` per-node lists (unreached = inf /
    unbounded) or ``None`` when disabled or no corridors exist."""
    g = APRON_CORRIDOR_SMOOTH_GRADE
    if g <= 0.0 or APRON_CORRIDOR_SMOOTH_RADIUS_M <= 0.0:
        return None
    segs = _corridor_segments(layout, include_roads=False)
    if not segs:
        return None
    n = len(nodes)
    INF = float("inf")
    cell = max(APRON_CORRIDOR_SEED_RADIUS_M, _RECT_SEED_MAX_D0_M)
    grid = _seg_grid(segs, cell)

    rect_nodes: set = set()
    near_nodes: set = set()
    for sc in shape_constraints:
        role = sc["role"]
        if role in SLOPING_RECT_ROLES and role != ROLE_SERVICE_ROAD:
            rect_nodes.update(sc["nodes"])
        elif role in (ROLE_APRON, ROLE_JUNCTION, ROLE_TERMINAL):
            near_nodes.update(sc["nodes"])
    # Airside union for the mid-range seed visibility test (apron lanes run
    # through apron INTERIORS — ring vertices sit 15-60 m away laterally;
    # they seed only when the connector to the lane stays inside pavement,
    # the true transverse offset.  Across grass = the misattribution this
    # whole function exists to kill).
    airside = None
    try:
        from shapely.geometry import LineString
        from shapely.ops import unary_union
        from shapely.prepared import prep
        polys = [s.polygon for s in layout.shapes
                 if s.role in PAVEMENT_ROLES
                 and s.polygon is not None and not s.polygon.is_empty]
        if polys:
            airside = prep(unary_union(polys).buffer(0.5))
    except _GEOM_EXC:
        airside = None
    seeds: dict[int, float] = {}
    for i in rect_nodes:
        if i < n:
            d0 = _corridor_point_distance(*nodes[i], segs, grid, cell)
            if d0 <= _RECT_SEED_MAX_D0_M:
                seeds[i] = d0
    for i in near_nodes:
        if i >= n or i in seeds:
            continue
        d0, px, py = _corridor_point_nearest(*nodes[i], segs, grid, cell)
        if d0 <= APRON_CORRIDOR_SEED_RADIUS_M:
            seeds[i] = d0
        elif d0 <= _RECT_SEED_MAX_D0_M and airside is not None:
            try:
                if airside.contains(
                        LineString((nodes[i], (px, py)))):
                    seeds[i] = d0
            except _GEOM_EXC:
                pass
    if not seeds:
        return None

    adj: dict[int, list[tuple[int, float]]] = {}
    for (i, j, _c) in all_edges:
        if i >= n or j >= n or i == j:
            continue
        (xa, ya), (xb, yb) = nodes[i], nodes[j]
        d = math.hypot(xa - xb, ya - yb)
        adj.setdefault(i, []).append((j, d))
        adj.setdefault(j, []).append((i, d))

    def _relax(init, scale):
        dist = list(init)
        pq = [(dv, i) for i, dv in enumerate(dist) if dv < INF]
        heapq.heapify(pq)
        while pq:
            dv, u = heapq.heappop(pq)
            if dv > dist[u]:
                continue
            for v, d in adj.get(u, ()):
                nd = dv + d * scale
                if nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist

    # NETWORK PROFILE MODEL (M6): seed VALUES are exact FIELD samples at
    # the nearest corridor point, not the vertex's own solved value — the
    # vertex-proximity seeding error (a rim vertex 10 m off the lane
    # seeded the field with its own stale level) disappears, and lanes
    # crossing apron interiors grade the apron from the profile that
    # actually runs through it (HECA #198 B/C class).
    npf = (getattr(layout, "_network_profile_field", None)
           if NETWORK_PROFILE_MODEL else None)
    init_d = [INF] * n
    init_hi = [INF] * n
    init_nlo = [INF] * n
    for i, d0 in sorted(seeds.items()):
        sv = elev[i]
        if npf is not None:
            d9, px9, py9 = _corridor_point_nearest(*nodes[i], segs, grid,
                                                   cell)
            if d9 < INF:
                fv, fgap = npf.sample(px9, py9)
                if fv is not None and fgap <= 10.0:
                    sv = fv
        init_d[i] = d0
        init_hi[i] = sv + g * d0
        init_nlo[i] = -(sv - g * d0)
    geo_dist = _relax(init_d, 1.0)
    c_hi = _relax(init_hi, g)
    c_lo = [(-x if x < INF else -INF) for x in _relax(init_nlo, g)]
    if _os.environ.get("O4_APZ_DEBUG") == "1":
        ap = {i for sc in shape_constraints if sc["role"] == ROLE_APRON
              for i in sc["nodes"] if i < n}
        R_dbg = APRON_CORRIDOR_SMOOTH_RADIUS_M
        in_z = [i for i in ap if geo_dist[i] <= R_dbg]
        near_miss = sum(1 for i in ap if R_dbg < geo_dist[i] <= 1.5 * R_dbg)
        far = sum(1 for i in ap if geo_dist[i] > 1.5 * R_dbg)
        viol = [(abs(min(max(elev[i], c_lo[i]), c_hi[i]) - elev[i]), i)
                for i in in_z]
        viol.sort(reverse=True)
        print(f"[apz] seeds={len(seeds)} apron-nodes={len(ap)} "
              f"in-zone={len(in_z)} near-miss(R..1.5R)={near_miss} "
              f"far={far} "
              f"band-pulls>0.1m={sum(1 for v, _ in viol if v > 0.1)} "
              f"max-pull={viol[0][0] if viol else 0:.2f}m")
    return geo_dist, c_lo, c_hi


def _lipschitz_tighten_bands(n, lo, hi, edges):
    """Tightest IMPLIED per-node bands under the local edge caps (s76
    junction-ripple root): the ROUTE bands are computed over the CENTERLINE
    graph, so two ring-adjacent vertices can enter that graph at different
    nodes and carry floors/ceilings differing by more than their own edge
    cap allows — the one-time band clamp then prints that discontinuity
    into the surface as a ripple.  The edge system already implies the
    smooth version: ``lo*_i = max_j (lo_j − capdist(i, j))`` and
    ``hi*_i = min_j (hi_j + capdist(i, j))`` over cap-weighted edge paths.
    One multi-source Dijkstra per side (every node seeds its own bound)
    makes the band field edge-Lipschitz — pure tightening, no new
    constraint: anything outside ``[lo*, hi*]`` violated the original
    system through some edge path anyway."""
    INF = float("inf")
    adj: dict[int, list[tuple[int, float]]] = {}
    for (i, j, c) in edges:
        adj.setdefault(i, []).append((j, c))
        adj.setdefault(j, []).append((i, c))

    def _relax(init):
        dist = list(init)
        pq = [(d, i) for i, d in enumerate(dist) if d < INF]
        heapq.heapify(pq)
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, c in adj.get(u, ()):
                nd = d + c
                if nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist

    hi2 = _relax([(x if x < INF else INF) for x in hi])
    nlo = _relax([(-x if x > -INF else INF) for x in lo])
    lo2 = [(-x if x < INF else -INF) for x in nlo]
    return lo2, hi2


def _fair_surface_ripples(elev, edges, is_hard, lo, hi, coupling,
                          held_extra, band_pinned, max_move,
                          sweeps=150, omega=0.5, tol=0.005) -> int:
    """FINAL FAIRING (s76, user in-sim feedback: "we used to have a final
    solver pass that distributed things and smoothed everything").  The old
    dense all-pair chord web acted as an implicit smoother — every vertex was
    tied to many far neighbours, so the band clamp + cap projection ironed
    sub-cap DEM noise flat.  With chords demoted to the local window
    (ROUTE_FIELD_MODEL) that side effect disappeared and junction/apron
    interiors keep visible sub-cap ripples.

    This pass restores the smoothing EXPLICITLY and lawfully: each soft,
    uncoupled vertex relaxes toward the inverse-cap-weighted average of its
    grade-graph neighbours (weight ∝ 1/distance — short edges dominate, so
    it kills local bumps first), clamped each sweep into

      * its route band ``[lo, hi]`` (the long-range law stays satisfied), and
      * a per-node displacement budget ``±max_move`` from its solved value
        (this is a RIPPLE smoother, not a re-leveller — without the budget a
        long Laplacian run drifts whole aprons toward the harmonic surface).

    HARD anchors, corridor-held writes, band-pinned nodes and COUPLED groups
    (rect flat ends, terminal levels — planes/levels, not ripple carriers)
    never move.  A short cap re-projection afterwards (caller) cleans any
    residual cap drift.  Returns the number of vertices moved > 1 cm.
    """
    INF = float("inf")
    n = len(elev)
    nbrs: dict[int, list[tuple[int, float]]] = {}
    for (i, j, c) in edges:
        if c <= 0.0:
            continue                      # flat pairs are coupled levels
        w = 1.0 / max(c, 0.05)
        nbrs.setdefault(i, []).append((j, w))
        nbrs.setdefault(j, []).append((i, w))
    fixed = [False] * n
    for i in range(n):
        if is_hard[i] or i in held_extra or i in band_pinned:
            fixed[i] = True
        elif coupling is not None and i in coupling and len(coupling[i]) > 1:
            fixed[i] = True               # rigid level group — not faired
    free = [i for i in nbrs if i < n and not fixed[i]]
    if not free:
        return 0
    orig = {i: elev[i] for i in free}
    for _ in range(sweeps):
        mx = 0.0
        for i in free:
            acc = 0.0
            wsum = 0.0
            for (j, w) in nbrs[i]:
                acc += w * elev[j]
                wsum += w
            if wsum <= 0.0:
                continue
            tgt = elev[i] + omega * (acc / wsum - elev[i])
            o = orig[i]
            if tgt > o + max_move:
                tgt = o + max_move
            elif tgt < o - max_move:
                tgt = o - max_move
            li, hi_i = lo[i], hi[i]
            if li > hi_i:
                continue                  # infeasible band — leave as is
            if hi_i < INF and tgt > hi_i:
                tgt = hi_i
            if li > -INF and tgt < li:
                tgt = li
            d = tgt - elev[i]
            if abs(d) > mx:
                mx = abs(d)
            elev[i] = tgt
        if mx < tol:
            break
    return sum(1 for i in free if abs(elev[i] - orig[i]) > 0.01)


def _grade_bands(n, elev, is_hard, edges):
    """Feasible elevation band ``[lo[v], hi[v]]`` for every node under the
    within-shape grade edges, by two multi-source Dijkstras from the HARD
    anchors over the cap-weighted constraint graph (user 2026-05-28, the direct
    difference-constraint solve).

    A grade limit ``|elev_i - elev_j| <= cap`` is a difference constraint, so
    the tightest UPPER bound is ``hi[v] = min over HARD anchors a of
    (elev[a] + shortest cap-weighted path a->v)`` and the lower bound is
    symmetric.  Crucially the ALL-``hi`` (and all-``lo``) assignment is itself
    feasible — every edge satisfies ``|hi[u]-hi[v]| <= cap`` by the triangle
    inequality — so a feasible surface always exists (no negative cycle, since
    caps >= 0).  Multiple paths are handled automatically: a node simply takes
    the tightest reaching anchor; we never enumerate routes.  ``lo[v] > hi[v]``
    flags a genuinely infeasible node (two HARD anchors closer in the graph than
    their elevation gap allows — e.g. a seam-pinned flat end).  Unreachable
    nodes get ``(-inf, +inf)`` (no anchor constraint).
    """
    adj: dict[int, list[tuple[int, float]]] = {}
    for (i, j, c) in edges:
        adj.setdefault(i, []).append((j, c))
        adj.setdefault(j, []).append((i, c))
    INF = float("inf")

    def _dijkstra(seed_sign):
        dist = [INF] * n
        pq: list[tuple[float, int]] = []
        for a in range(n):
            if is_hard[a]:
                dist[a] = seed_sign * elev[a]
                heapq.heappush(pq, (dist[a], a))
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, c in adj.get(u, ()):  # type: ignore[arg-type]
                nd = d + c
                if nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist

    hi = _dijkstra(+1.0)                       # hi[v] = min_a(elev[a] + d)
    nlo = _dijkstra(-1.0)                       # nlo[v] = min_a(-elev[a] + d)
    lo = [(-x if x != INF else -INF) for x in nlo]
    return lo, hi


# Difference-constraint within-shape enforcement budget.  The Dijkstra bands
# (``_grade_bands``) do the heavy lifting directly (O(E·log V), ~6 ms — the
# shortest cap-path IS the fully-propagated hard-anchor constraint, no iteration);
# the band-clamp then applies it, and the residual soft↔soft projection converges
# to its floor in ~1–2 k sweeps (it plateaus — more does nothing).  Anything left
# at the plateau is structural / anchor-pinned and only an anchor flex can fix it.
_WITHIN_ENFORCE_MAX_SWEEPS = 2000


def _interior_entry_dist(layout):
    """The shared INTERIOR-PATH entry measure (gate
    ``INTERIOR_PATH_ENTRIES``, docs/interior_path_entries.md): returns
    ``measure.distance`` or ``None`` (gate off / no airside).  Built
    ONCE per solve from the layout's airside union and cached on the
    layout — the field, the reach bands and (via the same module) the
    validator all share it, so law-graph parity holds by construction
    (partial application is the s78p5/s79 measured failure mode)."""
    from ..config import INTERIOR_PATH_ENTRIES
    if not INTERIOR_PATH_ENTRIES or layout is None:
        return None
    M = getattr(layout, "_interior_measure", None)
    if M is False:
        return None
    if M is None:
        from ..interior_path import AIRSIDE_MEASURE_ROLES, \
            measure_from_polys
        try:
            M = measure_from_polys(
                [s.polygon for s in layout.shapes
                 if s.role in AIRSIDE_MEASURE_ROLES
                 and s.polygon is not None
                 and not s.polygon.is_empty])
        except Exception:
            M = None
        layout._interior_measure = M if M is not None else False
        if M is None:
            return None
    return M.distance


def _runway_reach_bands(nodes, elev, runway_nodes, seam_nodes, all_edges, cap,
                        layout, extra_anchors=None, noise_frac=0.0,
                        graph=None, extra_points=None, entry_dist=None):
    """Per-node feasible band ``[lo, hi]`` from the runway/seam HARD anchors,
    where RUNWAY reachability is measured along the taxiway CENTERLINE route
    (``taxi_routing``) and SEAM reachability via the within-shape geodesic, then
    intersected.

    Why: the within-shape VISIBILITY geodesic shortcuts ACROSS a big apron's
    interior, so propagating a runway's elevation through it under-counts the real
    distance and FALSELY band-pins pavement caught between two runways at
    different levels (the recurring HECA "squeeze" — a measurement bug, not
    infeasibility; the real connection is the perimeter taxi route).  Measuring
    runway connections along the centerline (the path an aircraft actually takes,
    and the surface that actually carries the grade between runways) gives the
    correct, looser band.  The within-apron grade itself stays the visibility
    geodesic (enforced by the projection's edges); only the runway-REACHABILITY
    distance switches to the centerline.  Seam anchors stay geodesic (a tile-seam
    pins its LOCAL pavement directly, not via a taxi route).

    ROUTE-FIELD MODEL extensions (docs/route_field_model.md §5.2/§5.3 —
    these bands are THE long-range grade law for the enforce):

    * ``extra_anchors``: additional node indices anchored at their current
      ``elev`` values through the route graph — base_hard pins (thresholds,
      seam, boundary) and corridor-held writes (which threaded their own
      route bands, so anchoring on them is consistent; this replaces the
      p7/p10g ``band_exempt`` carve-out by construction).
    * ``noise_frac``: relative route-measurement margin — the route graph
      under-counts real routes ~4 % (straight endpoint stubs, uncurved row
      joins), so a band used as a HARD constraint must be relaxed by
      ``noise_frac·cap·route_d`` or it manufactures sub-metre tension
      against legal surfaces.  The validator carries the SAME margin.
    * ``graph``: a prebuilt ``TaxiRouteGraph`` (typically the shared
      runway-augmented instance) — callers that need threshold/runway-end
      reachability pass the augmented graph; default = the shared cached
      plain centerline graph (built once per solve).
    * ``extra_points``: ``[(x, y, value)]`` anchor POINTS that are not
      solver nodes — the NETWORK PROFILE MODEL's field vertices.  Dense
      field anchors give adjacent pavement vertices CONSISTENT bands
      (one far-away write entering the graph at two different nodes was
      the metre-scale band-entry noise class); they enter PLAIN-only,
      like every non-runway anchor.
    """
    from auto_patch.taxi_routing import shared_taxi_route_graph
    n = len(nodes)
    NEG, POS = float("-inf"), float("inf")
    lo = [NEG] * n
    hi = [POS] * n
    G = graph if graph is not None else shared_taxi_route_graph(layout)
    capm = cap * (1.0 + (noise_frac or 0.0))
    anchor_nodes = set(runway_nodes)
    if extra_anchors:
        anchor_nodes |= set(extra_anchors)
    if G.coord and (anchor_nodes or extra_points):
        # Cache each node's nearest centerline (key, gap) — one O(|coord|) scan
        # per node, reused for seeds and queries.  On an AUGMENTED graph,
        # pavement-vertex queries (and non-runway anchors) are restricted to
        # PLAIN taxi-row nodes: augmented runway-midline nodes are route
        # segments + RUNWAY-anchor entry points only — a vertex in a
        # route-graph coverage hole must get the hole's WEAK band, not a
        # tight fictitious band via a straight hop across non-pavement
        # (CYXY TX1, s76).
        near: dict[tuple, tuple] = {}

        def _near(i, plain_only):
            r = near.get((i, plain_only))
            if r is None:
                r = G.nearest_key(*nodes[i], plain_only=plain_only)
                if (entry_dist is not None and r[0] is not None
                        and r[1] > 0.5):
                    # interior-path entry gap ("no grade checks across
                    # grass" — docs/interior_path_entries.md); no
                    # in-pavement path = no band from this entry.
                    d9 = entry_dist(
                        (nodes[i][0], nodes[i][1]), G.coord[r[0]])
                    r = (None, r[1]) if d9 is None else (r[0], d9)
                near[(i, plain_only)] = r
            return r

        rwy_set = set(runway_nodes)

        def _propagate(sign):
            # Multi-source Dijkstra over the centerline graph (edge weight =
            # capm*length) seeded at each anchor's centerline entry.
            dist: dict = {}
            pq: list = []
            for a in anchor_nodes:
                if a >= n:
                    continue
                key, gap = _near(a, a not in rwy_set)
                if key is None:
                    continue
                v0 = sign * elev[a] + capm * gap
                if v0 < dist.get(key, POS):
                    dist[key] = v0
                    heapq.heappush(pq, (v0, key))
            for (xp, yp, vp) in (extra_points or ()):
                key, gap = G.nearest_key(xp, yp, plain_only=True)
                if key is None:
                    continue
                if entry_dist is not None and gap > 0.5:
                    gap = entry_dist((xp, yp), G.coord[key])
                    if gap is None:
                        continue
                v0 = sign * vp + capm * gap
                if v0 < dist.get(key, POS):
                    dist[key] = v0
                    heapq.heappush(pq, (v0, key))
            while pq:
                d, u = heapq.heappop(pq)
                if d > dist.get(u, POS):
                    continue
                for v, w in G.adj.get(u, ()):  # type: ignore[union-attr]
                    nd = d + capm * w
                    if nd < dist.get(v, POS):
                        dist[v] = nd
                        heapq.heappush(pq, (nd, v))
            return dist
        ceil_key = _propagate(1.0)            # elev[a] + capm*(gap+route)
        floor_key = _propagate(-1.0)          # -elev[a] + capm*(gap+route)
        for v in range(n):
            key, gap = _near(v, True)
            if key is None:
                continue
            c = ceil_key.get(key)
            if c is not None:
                hi[v] = c + capm * gap
            f = floor_key.get(key)
            if f is not None:
                lo[v] = -(f + capm * gap)
    # Seam anchors: keep the geodesic band (local pin), intersect.
    if seam_nodes:
        is_seam = [False] * n
        for i in seam_nodes:
            if i < n:
                is_seam[i] = True
        slo, shi = _grade_bands(n, elev, is_seam, all_edges)
        for v in range(n):
            if shi[v] < hi[v]:
                hi[v] = shi[v]
            if slo[v] > lo[v]:
                lo[v] = slo[v]
    return lo, hi


def _count_within_viol(elev, shape_constraints):
    """(count, worst_excess_m) of within-shape grade edges over cap — the model's
    own constraint set (NOT the geodesic emit metric), for per-step review."""
    n_v = 0
    worst = 0.0
    for sc in shape_constraints:
        for (i, j, c) in sc["edges"]:
            if c <= 0:
                continue
            ex = abs(elev[i] - elev[j]) - c
            if ex > 1e-4:
                n_v += 1
                if ex > worst:
                    worst = ex
    return n_v, worst


def _enforce_within_shape_grade(elev, shape_constraints, base_hard,
                                nodes=None, layout=None, bucket_to_idx=None,
                                icao=None, held_extra=None,
                                band_exempt=None) -> int:
    """FINAL within-shape grade ENFORCEMENT via the difference-constraint solve.

    Every within-shape limit ``|x_i - x_j| <= cap·d`` is a difference
    constraint.  With the runway/seam HARD anchors fixed, :func:`_grade_bands`
    gives each node its EXACT feasible band ``[lo, hi]`` by shortest cap-paths
    (Dijkstra, O(V·E) — no iteration), and the all-lo / all-hi assignments are
    themselves compliant (triangle inequality), so a feasible compliant surface
    ALWAYS exists EXCEPT where two hard anchors are closer in the graph than
    their elevation gap allows: ``lo > hi`` — a BAND-PINNED node, structurally
    infeasible at the fixed anchors.  Those are the runway-flex's job (an anchor
    must yield), NOT a within-shape failure, so we HOLD them at their current
    level and let the rest of the surface comply around them.

    The bulk correction is the one-time band-clamp inside
    :func:`_project_within_bands`; the remaining soft↔soft residual (apron
    interiors far from any hard anchor, where bands are loose) is projected to
    convergence.  Holding the band-pinned nodes out of the relaxation lets the
    feasible region converge cleanly (they no longer slosh the projection).

    Stays DEM-close: projects the EXISTING near-DEM surface onto the feasible
    polytope, never reseeds.  Returns the count of band-pinned (infeasible)
    nodes — the residual that only a runway/anchor flex can resolve.
    """
    n = len(elev)
    all_edges = [e for sc in shape_constraints for e in sc["edges"]]
    if not all_edges:
        return 0
    # Runway/seam HARD set: the whole settled runway (authoritative surface the
    # pavement grades to) + seam.
    is_hard = list(base_hard)
    runway_nodes: set = set()
    seam_nodes: set = set()
    if layout is not None and bucket_to_idx is not None:
        runway_nodes = _runway_node_set(layout, bucket_to_idx)
        seam_nodes = _seam_pinned_runway_nodes(layout, bucket_to_idx)
        for i in runway_nodes | seam_nodes:
            if i < n:
                is_hard[i] = True
    # Feasible bands: RUNWAY reachability via the taxiway CENTERLINE route (the
    # within-shape geodesic shortcuts across big aprons and falsely band-pins
    # pavement between two runways — the HECA "squeeze" is this measurement bug,
    # NOT infeasibility).  Falls back to the geodesic band when node coords aren't
    # available (older callers).
    if nodes is not None and layout is not None and runway_nodes:
        if ROUTE_FIELD_MODEL:
            # ROUTE-FIELD MODEL (#3): these bands ARE the long-range grade
            # law.  Anchor set per docs/route_field_model.md §5.3 — runway
            # nodes + base_hard pins + corridor-held writes, all at their
            # current values, measured over the runway-AUGMENTED route
            # graph with the §5.2 noise margin.
            from auto_patch.taxi_routing import runway_augmented_route_graph
            extra = {i for i in range(n) if base_hard[i]}
            if held_extra:
                extra |= {i for i in held_extra if i < n}
            # NETWORK PROFILE MODEL: the solved field's plain vertices
            # join the anchor set — DENSE anchors give adjacent vertices
            # consistent bands (a single far-away held write entering the
            # route graph at two different nodes printed metre-scale
            # band-entry noise into apron rims — CYXY #68/#63), and the
            # long-range law becomes the FIELD by construction (M6).
            field_pts = None
            band_graph = runway_augmented_route_graph(layout)
            npf9 = (getattr(layout, "_network_profile_field", None)
                    if NETWORK_PROFILE_MODEL else None)
            if npf9 is not None:
                field_pts = npf9.vertices_with_values()
                # the law measures on the FIELD's graph — it contains
                # every lane the field knows (apt rows, discovered axes,
                # midlines), so bands cannot under-connect relative to
                # the solve (discovered-lane areas had NO consistent
                # entry into the apt-only graph — CYXY #63)
                band_graph = npf9.route_graph_view()
            lo, hi = _runway_reach_bands(
                nodes, elev, runway_nodes, seam_nodes, all_edges,
                TAXI_MAX_GRADE, layout, extra_anchors=extra,
                noise_frac=_ROUTE_NOISE_FRAC,
                graph=band_graph,
                extra_points=field_pts,
                entry_dist=_interior_entry_dist(layout))
            # Route bands live on the CENTERLINE graph; make the band field
            # edge-Lipschitz before clamping or adjacent vertices print
            # their graph-entry discontinuities into the surface as
            # ripples (pure tightening — see _lipschitz_tighten_bands).
            if _os.environ.get("O4_BAND_NODES"):
                for i9 in (int(t) for t in
                           _os.environ["O4_BAND_NODES"].split(",")):
                    if i9 < n:
                        print(f"[band] pre-tighten n{i9} "
                              f"lo={lo[i9]:.2f} hi={hi[i9]:.2f} "
                              f"elev={elev[i9]:.2f}")
            lo, hi = _lipschitz_tighten_bands(n, lo, hi, all_edges)
            if _os.environ.get("O4_BAND_NODES"):
                for i9 in (int(t) for t in
                           _os.environ["O4_BAND_NODES"].split(",")):
                    if i9 < n:
                        print(f"[band] post-tighten n{i9} "
                              f"lo={lo[i9]:.2f} hi={hi[i9]:.2f}")
        else:
            lo, hi = _runway_reach_bands(
                nodes, elev, runway_nodes, seam_nodes, all_edges,
                TAXI_MAX_GRADE, layout,
                entry_dist=_interior_entry_dist(layout))
    else:
        lo, hi = _grade_bands(n, elev, is_hard, all_edges)
    # corridor-touched junctions: the held corridor profile is the route
    # truth there; per-vertex route bands (route-graph artifacts) would
    # pin free vertices metres off the held writes — exempt them and let
    # the visibility-edge projection conform them to the corridor.
    # Under ROUTE_FIELD_MODEL the exemption is NOT applied: corridor-held
    # writes are IN the band anchor set, so the route-band-vs-corridor
    # fight it papered over disappears by construction (§5.4; machinery
    # kept for the gate-off path — delete only with measurements).
    if band_exempt and not ROUTE_FIELD_MODEL:
        for i in band_exempt:
            if i < n and not is_hard[i]:
                lo[i] = float("-inf")
                hi[i] = float("inf")
    band_pinned = {i for i in range(n) if lo[i] > hi[i] + 1e-6}
    # PINNED-NODE LEAST-VIOLATION PLACEMENT (s77p3, user: "aprons are
    # allowing some extreme dips — they can't just follow terrain"):
    # a band-pinned node (floor above ceiling — the squeeze families) is
    # held through the projections, but holding it at its RELIEF value
    # leaves it wherever DEM put it — HECA #193's valley vertex sat at
    # 99.46 with band [104.14 lo, 102.38 hi]: 2.9 m below even the
    # CEILING, violating both laws by more than necessary.  Any value
    # outside [hi, lo] is Pareto-worse than the nearest interval edge;
    # clamp into the inverted interval (nearest edge keeps the move
    # minimal and inherits the band fields' edge-Lipschitz smoothness).
    # The projections below then conform free neighbours around the
    # lifted values.
    if WRITE_ARBITRATION:
        held0 = held_extra or ()
        for i in band_pinned:
            if is_hard[i] or i in held0:
                continue
            v0 = min(max(elev[i], hi[i]), lo[i])
            if v0 != elev[i]:
                elev[i] = v0
        if NETWORK_PROFILE_MODEL and band_pinned:
            # nearest-edge placement is per-node: two adjacent pinned
            # nodes can land on OPPOSITE edges of their inverted
            # intervals and print the inversion width as a wall (SPJC
            # #92: 0.7 m over 5.7 m at a zone boundary).  Cap-project
            # edges touching pinned nodes, each end clamped INSIDE its
            # own inverted interval [hi, lo] — every value there is
            # "between the laws", so smoothing among them loses nothing.
            for _sw0 in range(60):
                mx0 = 0.0
                for (i0, j0, c0) in all_edges:
                    if c0 <= 0.0:
                        continue
                    pi = i0 in band_pinned and not is_hard[i0] \
                        and i0 not in held0
                    pj = j0 in band_pinned and not is_hard[j0] \
                        and j0 not in held0
                    if not (pi or pj):
                        continue
                    d0 = elev[i0] - elev[j0]
                    ex0 = abs(d0) - c0
                    if ex0 <= 0.01:
                        continue
                    s0 = 1.0 if d0 > 0 else -1.0
                    if pi:
                        nv = elev[i0] - s0 * (ex0 if not pj else ex0 / 2)
                        elev[i0] = min(max(nv, hi[i0]), lo[i0])
                    if pj:
                        nv = elev[j0] + s0 * (ex0 if not pi else ex0 / 2)
                        elev[j0] = min(max(nv, hi[j0]), lo[j0])
                    mx0 = max(mx0, ex0)
                if mx0 <= 0.01:
                    break
    if _os.environ.get("O4_TRACE_LL") and nodes is not None \
            and layout is not None:
        hard_plus = list(is_hard)
        for i9 in (held_extra or ()):
            if i9 < n:
                hard_plus[i9] = True
        glo9, ghi9 = _grade_bands(n, elev, hard_plus, all_edges)
        # provenance Dijkstra: which anchor BINDS the lo side
        adj9: dict = {}
        for (i2, j2, c2) in all_edges:
            adj9.setdefault(i2, []).append((j2, c2))
            adj9.setdefault(j2, []).append((i2, c2))
        dist9 = [float("inf")] * n
        src9: list = [None] * n
        pq9: list = []
        for a2 in range(n):
            if hard_plus[a2]:
                dist9[a2] = -elev[a2]
                src9[a2] = a2
                heapq.heappush(pq9, (dist9[a2], a2))
        while pq9:
            d2, u2 = heapq.heappop(pq9)
            if d2 > dist9[u2]:
                continue
            for v2, c2 in adj9.get(u2, ()):
                nd2 = d2 + c2
                if nd2 < dist9[v2]:
                    dist9[v2] = nd2
                    src9[v2] = src9[u2]
                    heapq.heappush(pq9, (nd2, v2))
        for part9 in _os.environ["O4_TRACE_LL"].split(";"):
            try:
                la9, lo9q = (float(x) for x in part9.split(","))
                xq, yq = layout.ll_to_m(la9, lo9q)
            except _GEOM_EXC:
                continue
            for i9, (xn9, yn9) in enumerate(nodes):
                if (xn9 - xq) ** 2 + (yn9 - yq) ** 2 <= 2.25:
                    s9 = src9[i9]
                    sinfo = "-"
                    if s9 is not None:
                        sx9, sy9 = nodes[s9]
                        sll = layout.m_to_ll(sx9, sy9)
                        sinfo = (f"n{s9}@({sll[0]:.6f},{sll[1]:.6f}) "
                                 f"e={elev[s9]:.2f} "
                                 f"hard={is_hard[s9]} "
                                 f"held={s9 in (held_extra or ())} "
                                 f"cap-path="
                                 f"{elev[s9] + dist9[i9]:.2f}m")
                    print(f"[trace] enforce n{i9} e={elev[i9]:.2f} "
                          f"routeband=[{lo[i9]:.2f},{hi[i9]:.2f}] "
                          f"edgeband=[{glo9[i9]:.2f},{ghi9[i9]:.2f}] "
                          f"hard={is_hard[i9]} "
                          f"held={i9 in (held_extra or ())} "
                          f"pinned={i9 in band_pinned} "
                          f"lo-anchor: {sinfo}")
    # Terminals YIELD in the final enforce instead of being frozen at their
    # STEP-2 level (user model phase 1/2: terminals move to the level the
    # connecting aprons need).  Freezing them held the worst HECA squeeze open —
    # stub A4 bridges a LOW runway (05L) to an apron pinned high by terminal7;
    # terminal7 frozen at 76.4 left A4 at 21.8 %.  Letting terminal7 yield down
    # as a RIGID flat unit (it stays single-level) resolves it.  Each terminal is
    # a rigid level group (cap-0 flat shapes unioned by shared node — edge-
    # connected terminals co-level, session 59); the group moves together, so
    # flatness is preserved, and the runway-reach band clamps its travel (bounded
    # ~5 m, no slosh — the band-clamp is what the earlier free-terminal attempts
    # lacked).  A group sharing a hard node is still held (one corner pinned).
    coupling = _merge_terminal_level_groups(
        _build_level_coupling(shape_constraints), shape_constraints)
    # TERMINALS MUST NOT RISE (standing ruling; terminal7 ≈ 70).  In
    # rigid-flat mode (TERMINAL_PADS_SLOPE False) pads enter the enforce at
    # their taxi-route-seeded + relief levels; the band clamp/sweeps must
    # never lift them (the Lipschitz-implied floors near a high corridor
    # dragged HECA terminal7 70 → 72.9 in the first s76 build — that
    # tension is the runway-flex/arbitration layer's job, like any other
    # anchor squeeze).  YIELD-DOWN stays legal: each pad level group is
    # clamped down to its band CEILING here, then the pads are HELD through
    # every projection below so nothing can drag them back up.
    _term_nodes: set = set()
    _tdbg = _os.environ.get("O4_TERM_DEBUG") == "1"
    if not TERMINAL_PADS_SLOPE and TERMINAL_LEAF_LEVELS:
        # TERMINAL LEAF LEVELS (s77 user ruling, supersedes "terminals
        # must not rise"): pads are natural LEAF nodes — rigid-flat, and
        # their LEVEL follows the apron(s) they connect to, up or down,
        # not a pre-calculated seed ceiling or lock.  Mechanism: pads
        # enter the projections at their own MEDIAN (coherence only — no
        # seed ceiling) and stay HELD through them (free-following was
        # MEASURED-REJECTED twice: cap edges bind at the WORST single
        # neighbour, so route-pinned apron edges drag the whole rigid
        # pad — s76 terminal7 70→72.9, s77 leaf-v1 terminal1
        # 99.7→106.1); the actual LEAF re-level happens after the apron
        # solve below — the pad takes the MEDIAN of its adjacent apron
        # surface (robust to pinned outliers), then the legal
        # re-projection conforms the aprons around it.
        for sc2 in shape_constraints:
            if sc2["role"] == ROLE_TERMINAL:
                _term_nodes.update(sc2["nodes"])
        seen_g0: set = set()
        for i in _term_nodes:
            if i >= n:
                continue
            grp = coupling[i] if (coupling is not None and i in coupling) \
                else (i,)
            key = tuple(sorted(grp))
            if key in seen_g0:
                continue
            seen_g0.add(key)
            vals = sorted(elev[m] for m in grp if m < n)
            if not vals:
                continue
            lvl = vals[len(vals) // 2]
            if _tdbg and (vals[-1] - vals[0]) > 0.05:
                print(f"[term] leaf entry re-level grp({len(grp)}) "
                      f"{vals[0]:.2f}..{vals[-1]:.2f} -> {lvl:.2f}")
            for m in grp:
                if m < n and not is_hard[m]:
                    elev[m] = lvl
        # internal kink smoothing for SLOPED (squeezed) pads — the s76p3
        # coherence fix, SYMMETRIC under the leaf ruling (pads may move
        # either way; the relief leaves 1-3 m mixed-authority kinks that
        # the hold below would otherwise print)
        for sc2 in shape_constraints:
            if sc2["role"] != ROLE_TERMINAL or not sc2["edges"]:
                continue
            for _ in range(200):
                mx2 = 0.0
                for (i2, j2, c2) in sc2["edges"]:
                    if c2 <= 0.0:
                        continue
                    d2 = elev[i2] - elev[j2]
                    ex2 = abs(d2) - c2
                    if ex2 <= 1e-4:
                        continue
                    s2 = 1.0 if d2 > 0 else -1.0
                    h_i, h_j = is_hard[i2], is_hard[j2]
                    if h_i and h_j:
                        continue
                    if h_i:
                        elev[j2] += s2 * ex2
                    elif h_j:
                        elev[i2] -= s2 * ex2
                    else:
                        elev[i2] -= s2 * ex2 / 2.0
                        elev[j2] += s2 * ex2 / 2.0
                    if ex2 > mx2:
                        mx2 = ex2
                if mx2 <= 1e-4:
                    break
    elif not TERMINAL_PADS_SLOPE:
        for sc2 in shape_constraints:
            if sc2["role"] == ROLE_TERMINAL:
                _term_nodes.update(sc2["nodes"])
                if _tdbg:
                    print(f"[term] sc ref={sc2['ref']} flat={sc2['flat']} "
                          f"nodes={len(sc2['nodes'])} "
                          f"edges={len(sc2['edges'])}")
        seed_ceil = (getattr(layout, "_terminal_seed_ceiling", None)
                     if layout is not None else None) or {}
        seen_g: set = set()
        for i in _term_nodes:
            if i >= n:
                continue
            grp = coupling[i] if (coupling is not None and i in coupling) \
                else (i,)
            key = tuple(sorted(grp))
            if key in seen_g:
                continue
            seen_g.add(key)
            # Group ceiling = band ceiling ∩ the TAXI-ROUTE SEED levels
            # (the ruling's reference level — the relief may have lifted
            # the pad above its seed toward high aprons; pull it back).
            ceil_seed = min((seed_ceil.get(m, float("inf"))
                             for m in grp if m < n), default=float("inf"))
            ghi = min((hi[m] for m in grp if m < n), default=float("inf"))
            glo = max((lo[m] for m in grp if m < n), default=float("-inf"))
            # RE-LEVEL + yield-down in one move: the relief can leave a
            # flat pad internally INCONSISTENT (a shared node dragged low
            # by an apron — HECA terminal2 carried a 1.2 m single-vertex
            # dip, terminal1 0.2 m; the freeze below would print them).
            # The pad's level = its PREVAILING (median) value, capped at
            # the band ceiling when the group band is FEASIBLE and always
            # at the taxi-route SEED ceiling — pad COHERENCE is not a
            # "rise".  When the group band is INFEASIBLE (a big pad's
            # member floors cross its ceilings — the squeeze: HECA
            # terminal1's 88-node group), the bands are the ARBITRATION
            # residue, not a reason to freeze an incoherent pad: still
            # re-level at min(median, seed).  Single-node groups (sloped
            # squeezed pads) reduce to the plain per-node yield-down.
            vals = sorted(elev[m] for m in grp if m < n)
            med = vals[len(vals) // 2]
            if glo <= ghi:
                lvl = min(med, ghi, ceil_seed)
            else:
                lvl = min(med, ceil_seed)
            if _tdbg and (len(grp) > 1 or abs(lvl - vals[0]) > 0.05):
                print(f"[term] re-level grp({len(grp)}) "
                      f"{vals[0]:.2f}..{vals[-1]:.2f} -> {lvl:.2f} "
                      f"(ghi={ghi:.2f} glo={glo:.2f} seed={ceil_seed:.2f})")
            for m in grp:
                if m < n and not is_hard[m]:
                    elev[m] = lvl
        # DOWN-ONLY internal pad smoothing: the relief leaves kinks inside
        # squeezed pads (mixed-authority shared nodes — HECA terminal1
        # 0.20 m at 0.7 m, terminal2 1.2 m at 5.8 m), and the freeze below
        # would preserve them.  Resolve each over-cap pad pair by lowering
        # the HIGHER node only (never lift — the ruling); the pad ends
        # cap-smooth at or below its ceilings, and neighbouring aprons
        # re-conform around the frozen result in the projections below.
        for sc2 in shape_constraints:
            if sc2["role"] != ROLE_TERMINAL or not sc2["edges"]:
                continue
            n_moves2 = 0
            for _ in range(200):
                mx2 = 0.0
                for (i2, j2, c2) in sc2["edges"]:
                    if c2 <= 0.0:
                        continue
                    d2 = elev[i2] - elev[j2]
                    ex2 = abs(d2) - c2
                    if ex2 <= 1e-4:
                        continue
                    hi_n2, lo_n2 = (i2, j2) if d2 > 0 else (j2, i2)
                    if is_hard[hi_n2]:
                        continue
                    elev[hi_n2] = elev[lo_n2] + c2
                    n_moves2 += 1
                    if ex2 > mx2:
                        mx2 = ex2
                if mx2 <= 1e-4:
                    break
            if _tdbg and n_moves2:
                print(f"[term] down-only ref={sc2['ref']} "
                      f"moves={n_moves2}")
    held_all = set(held_extra or set()) | _term_nodes
    _dbg = _os.environ.get("O4_ENFORCE_DEBUG") == "1"
    if _dbg:
        v0 = sum(1 for (i, j, c) in all_edges
                 if c > 0 and abs(elev[i] - elev[j]) > c + 1e-4)

    def _bn_dump(tag9):
        if _os.environ.get("O4_BAND_NODES"):
            ids9 = [int(t) for t in
                    _os.environ["O4_BAND_NODES"].split(",")]
            for i9 in ids9:
                if i9 < n:
                    print(f"[band] {tag9} n{i9} e={elev[i9]:.2f} "
                          f"lo={lo[i9]:.2f} hi={hi[i9]:.2f} "
                          f"pinned={i9 in band_pinned} "
                          f"held={i9 in held_all} hard={is_hard[i9]}")
            if len(ids9) == 2 and tag9 == "pre-project":
                pr9 = (min(ids9), max(ids9))
                hit9 = any({i0, j0} == set(ids9)
                           for (i0, j0, _c0) in all_edges)
                print(f"[band] pair {pr9} in all_edges: {hit9}")
    _bn_dump("pre-project")
    sweeps, resid = _project_within_bands(
        elev, all_edges, is_hard, lo, hi, coupling,
        held_extra=held_all,
        max_sweeps=_WITHIN_ENFORCE_MAX_SWEEPS,
        tol=_SPREAD_COMPLY_TOL_M)
    _bn_dump("post-project")
    # APRON CORRIDOR SMOOTHING (best-effort 1 % near taxi corridors — see
    # _apron_corridor_zone_edges / _apron_corridor_geodesic_state): a
    # bounded projection on the tightened apron-zone edges, run AFTER the
    # legal enforce so it can only smooth within the already-feasible
    # region (bands still clamp every move).  Pads stay held (smoothing a
    # slope toward 1 % between high anchors and low free pavement LIFTS
    # the low side — pads dragged up 2.5 m through shared nodes in the
    # first build).  Under APRON_CORRIDOR_GEODESIC the zone is measured
    # by interior path and in-zone apron vertices are additionally
    # clamped into corridor-VALUE bands intersected with the legal route
    # bands (legal wins whenever the intersection is empty — the
    # route-pinned squeeze families YIELD, s77).
    n_zone = 0
    n_geo_tight = 0
    geo_leaf = None          # (geo_dist, c_lo) for the leaf lower bound
    if nodes is not None and layout is not None:
        geo_state = (_apron_corridor_geodesic_state(
                         layout, nodes, elev, shape_constraints, all_edges)
                     if APRON_CORRIDOR_GEODESIC else None)
        if geo_state is not None:
            geo_dist, c_lo, c_hi = geo_state
            geo_leaf = (geo_dist, c_lo)
            R_z = APRON_CORRIDOR_SMOOTH_RADIUS_M
            # Pair-smoothing ZONE = straight-line ∪ interior-path radius
            # (strictly additive over the in-sim-validated s76 coverage:
            # the pair caps reference no corridor value, so attribution
            # cannot mislead them; the interior path only GOVERNS the
            # value bands below).
            eu_zone = {e2 for e2 in _apron_corridor_zone_edges(
                layout, nodes, shape_constraints)}
            zone_edges = list({*eu_zone, *_apron_zone_scaled_edges(
                shape_constraints, lambda i: geo_dist[i] <= R_z)})
            zone_edges.sort()
            if zone_edges:
                n_zone = len(zone_edges)
                lo2 = list(lo)
                hi2 = list(hi)
                # TWO-RATE long-range apron law (s77p3, user: "aprons are
                # allowing some extreme dips — they can't just follow
                # terrain when distant from a taxiway"): the corridor
                # VALUE bands apply at 1 % within the smoothing zone and
                # at the LEGAL apron grade beyond it, over the full
                # interior-path field — with chords windowed at 80 m and
                # the route bands holed, distant apron interiors had NO
                # long-range constraint at all (HECA #193: ring 89.6 to
                # 110.1, a 9.9 m DEM valley between vertices 126-200 m
                # apart).  The extra slack reconstructs the legal-rate
                # law on the geodesic-shortest path.
                cap_l = _role_grade(ROLE_APRON)
                g_z = APRON_CORRIDOR_SMOOTH_GRADE
                for sc3 in shape_constraints:
                    if sc3["role"] != ROLE_APRON:
                        continue
                    for i in sc3["nodes"]:
                        if i >= n or geo_dist[i] == float("inf"):
                            continue
                        if lo2[i] > hi2[i]:
                            continue          # band-pinned: arbitration's job
                        extra = max(0.0, geo_dist[i] - R_z) \
                            * max(0.0, cap_l - g_z)
                        # Corridor band CLAMPED INTO the legal band (not
                        # intersected-or-dropped): when the corridor value
                        # is unreachable legally (route floors/ceilings —
                        # the squeeze families), the band collapses onto
                        # the nearest legal edge, moving the apron as
                        # CLOSE to its corridor as the law allows instead
                        # of leaving it at DEM height.
                        tlo = max(lo2[i], min(c_lo[i] - extra, hi2[i]))
                        thi = min(hi2[i], max(c_hi[i] + extra, lo2[i]))
                        if (tlo, thi) != (lo2[i], hi2[i]):
                            lo2[i] = tlo
                            hi2[i] = thi
                            n_geo_tight += 1
                _project_within_bands(
                    elev, zone_edges, is_hard, lo2, hi2, coupling,
                    held_extra=held_all,
                    max_sweeps=800, tol=_SPREAD_COMPLY_TOL_M)
        else:
            zone_edges = _apron_corridor_zone_edges(
                layout, nodes, shape_constraints)
            if zone_edges:
                n_zone = len(zone_edges)
                _project_within_bands(
                    elev, zone_edges, is_hard, lo, hi, coupling,
                    held_extra=held_all,
                    max_sweeps=800, tol=_SPREAD_COMPLY_TOL_M)
    _bn_dump("post-zone")
    # LEAF RE-LEVEL (TERMINAL_LEAF_LEVELS, s77 user ruling): with the
    # aprons solved and corridor-smoothed, each pad group takes the
    # MEDIAN of its adjacent apron surface — up or down.  The median is
    # robust against route-pinned outlier edges (terminal1's boundary is
    # mostly ~99-101 with a 104+ stretch near 23C; terminal7's
    # neighbours sit ~70 where its squeeze-band midpoint said 71.7).
    # The legal re-projection below then conforms the aprons around the
    # moved pads (pads stay held).
    n_leaf = 0
    if not TERMINAL_PADS_SLOPE and TERMINAL_LEAF_LEVELS and _term_nodes:
        pad_set = {i for i in _term_nodes if i < n}
        nbr_l: dict = {}
        for sc3 in shape_constraints:
            if sc3["role"] != ROLE_APRON:
                continue
            for (i3, j3, _c3) in sc3["edges"]:
                a3 = i3 in pad_set
                b3 = j3 in pad_set
                if a3 == b3:
                    continue
                p3, q3 = (i3, j3) if a3 else (j3, i3)
                if q3 >= n:
                    continue
                if nodes is not None:
                    (x3, y3), (x4, y4) = nodes[p3], nodes[q3]
                    if math.hypot(x3 - x4, y3 - y4) > 40.0:
                        continue
                nbr_l.setdefault(p3, set()).add(q3)
        seen_g1: set = set()
        for i in sorted(pad_set):
            grp = coupling[i] if (coupling is not None and i in coupling) \
                else (i,)
            key = tuple(sorted(grp))
            if key in seen_g1:
                continue
            seen_g1.add(key)
            if any(m < n and is_hard[m] for m in grp):
                continue
            nv = sorted({elev[b] for m in grp for b in nbr_l.get(m, ())})
            cv = sorted(elev[m] for m in grp if m < n)
            if not nv or not cv:
                continue
            target = nv[len(nv) // 2]
            # PAD 1 %-PLANE LOWER BOUND (user 2026-06-11: terminal1/2 sat
            # in a bowl — the apron sloped down hard to reach the pads;
            # "better to have a bigger deviation on the groundside, and
            # keep a smooth 1 % sloping apron away from the terminal").
            # The adjacent-apron MEDIAN follows the bowl the pad itself
            # dragged; bound it from below by the corridor 1 %-plane
            # value at the pad face (c_lo = corridor − 1 %·interior d),
            # relaxed by the two-rate slack beyond the smoothing zone so
            # distant user-approved pads (terminal4/5/8) keep their
            # levels.
            if NETWORK_PROFILE_MODEL and geo_leaf is not None:
                gd9, clo9 = geo_leaf
                cap9 = _role_grade(ROLE_APRON)
                g9 = APRON_CORRIDOR_SMOOTH_GRADE
                los9 = []
                for m in grp:
                    for b in nbr_l.get(m, ()):
                        if b >= n or clo9[b] <= float("-inf"):
                            continue
                        if gd9[b] == float("inf"):
                            continue
                        extra9 = max(0.0, gd9[b]
                                     - APRON_CORRIDOR_SMOOTH_RADIUS_M) \
                            * max(0.0, cap9 - g9)
                        los9.append(clo9[b] - extra9)
                # ≥8 samples: a bound from a handful of adjacent verts
                # is one pinned outlier away from re-litigating settled
                # pad levels (terminal7 ≈ 70 is an explicit ruling; the
                # first cut raised it 70.3 → 72.9 off ONE neighbour)
                if len(los9) >= 8:
                    los9.sort()
                    lo_b9 = los9[len(los9) // 2]
                    if target < lo_b9:
                        if _tdbg:
                            print(f"[term] leaf 1%-plane bound "
                                  f"{target:.2f} -> {lo_b9:.2f}")
                        target = lo_b9
            cur = cv[len(cv) // 2]
            move = target - cur
            if abs(move) < 0.02:
                continue
            move = max(-_LEAF_MAX_MOVE_M, min(_LEAF_MAX_MOVE_M, move))
            for m in grp:
                if m < n:
                    elev[m] = cur + move
            n_leaf += 1
            if _tdbg:
                print(f"[term] leaf follow grp({len(grp)}) "
                      f"{cur:.2f} -> {cur + move:.2f} "
                      f"(apron median {target:.2f}, "
                      f"{len(nv)} neighbour values)")
    # FINAL FAIRING + cap re-projection: iron sub-cap ripples the windowed
    # chord web no longer smooths implicitly (see _fair_surface_ripples),
    # then re-project caps so the smoothing cannot leave a new violation.
    _bn_dump("post-leaf")
    n_faired = 0
    if SURFACE_FAIRING:
        n_faired = _fair_surface_ripples(
            elev, all_edges, is_hard, lo, hi, coupling,
            held_extra=held_all,
            band_pinned=band_pinned,
            max_move=SURFACE_FAIRING_MAX_MOVE_M)
    if n_faired or n_zone or n_leaf:
        # restore strict LEGAL-cap feasibility after the preference passes
        # (a zone/fairing/leaf move can over-steepen a pair with an
        # out-of-zone or unfaired neighbour); pads still held.
        _project_within_bands(
            elev, all_edges, is_hard, lo, hi, coupling,
            held_extra=held_all,
            max_sweeps=400, tol=_SPREAD_COMPLY_TOL_M)
    if NETWORK_PROFILE_MODEL:
        # FINAL strict pair-law closure (the validator's own metric): a
        # band-pinned placement or a CORRIDOR WRITE carries the
        # route-metric / field verdict, but a HARD anchor and the local
        # pair cap outrank both (CYXY #63: a runway corner at 694.1 with
        # pinned rims held at 696.3 = a 2.2 m wall the projection was
        # forbidden to close; SPJC #92: a mid-rect body lerp deviating
        # from the bent field squeezed a free vertex between two held
        # writes).  Corridor writes MOVABLE, pads held (rigid leaf
        # ruling), and every move CLAMPED to ±1 m of the settled value —
        # the residual seams are sub-metre, and an unbounded final pass
        # is the SOR-divergence the band machinery exists to prevent
        # (measured: SPLP slid to 181 violations unbounded).  "Zero
        # violations" outranks the written profile at the seam (user
        # 2026-06-11).
        # ±2.5 m: enough to finish a squeeze redistribution (the ±1
        # first cut left taxiway-G rect planes 0.8 m over-cap at HECA
        # — the low mouth hit its budget mid-move), small enough that
        # infeasible-hard-anchor sites (bad input data) stay local.
        _cl_lo = [elev[i9] - 2.5 for i9 in range(n)]
        _cl_hi = [elev[i9] + 2.5 for i9 in range(n)]
        _held9 = [False] * n
        for i9 in range(n):
            grp9 = (coupling[i9] if (coupling is not None
                                     and i9 in coupling) else (i9,))
            _held9[i9] = any(is_hard[m9] or m9 in _term_nodes
                             for m9 in grp9)

        def _gmove9(i9, dd9):
            # move i9's coupled group together, delta clamped into the
            # tightest member's ±budget
            grp9 = (coupling[i9] if (coupling is not None
                                     and i9 in coupling) else (i9,))
            for m9 in grp9:
                dd9 = min(max(dd9, _cl_lo[m9] - elev[m9]),
                          _cl_hi[m9] - elev[m9])
            for m9 in grp9:
                elev[m9] += dd9
            return abs(dd9)

        for _sw9 in range(800):
            mx9 = 0.0
            for (i9, j9, c9) in all_edges:
                if c9 <= 0.0:
                    continue
                d9 = elev[i9] - elev[j9]
                ex9 = abs(d9) - c9
                if ex9 <= 0.0:
                    continue
                hi9, hj9 = _held9[i9], _held9[j9]
                if hi9 and hj9:
                    continue
                s9 = 1.0 if d9 > 0 else -1.0
                if hi9:
                    mx9 = max(mx9, _gmove9(j9, s9 * ex9))
                elif hj9:
                    mx9 = max(mx9, _gmove9(i9, -s9 * ex9))
                else:
                    mx9 = max(mx9, _gmove9(i9, -s9 * ex9 / 2.0))
                    mx9 = max(mx9, _gmove9(j9, s9 * ex9 / 2.0))
            if mx9 < _SPREAD_COMPLY_TOL_M:
                break
    _bn_dump("post-final")
    if _dbg:
        v1 = sum(1 for (i, j, c) in all_edges
                 if c > 0 and abs(elev[i] - elev[j]) > c + 1e-4)
        print(f"[enforce] {icao}: {sweeps} sweeps, edge-viol {v0}->{v1}, "
              f"residual {resid:.3f} m, band-pinned = {len(band_pinned)}, "
              f"apron-zone edges = {n_zone} "
              f"(geo-tightened {n_geo_tight}), faired = {n_faired}")
    return len(band_pinned)


def _shape_hop_depth(shape_constraints, seed_node_set) -> list[int]:
    """BFS per-shape hop distance from the seed-node set (typically the
    runway's nodes).  A shape touching a seed node is depth 1; each
    additional shape in the chain adds 1.  Shape adjacency = shapes that
    share at least one vertex (graph edges through ``node_owners``)."""
    node_owners: dict[int, list[int]] = {}
    for k, sc in enumerate(shape_constraints):
        for i in sc["nodes"]:
            node_owners.setdefault(i, []).append(k)
    INF_D = 1 << 30
    depth = [INF_D] * len(shape_constraints)
    dq: deque[int] = deque()
    for k, sc in enumerate(shape_constraints):
        if any(i in seed_node_set for i in sc["nodes"]):
            depth[k] = 1
            dq.append(k)
    while dq:
        k = dq.popleft()
        for i in shape_constraints[k]["nodes"]:
            for k2 in node_owners.get(i, ()):
                if k2 != k and depth[k2] > depth[k] + 1:
                    depth[k2] = depth[k] + 1
                    dq.append(k2)
    return depth


def _runway_node_set(layout, bucket_to_idx) -> set:
    """Return the set of node indices that belong to a runway / runway-
    crossing shape.  These are the BFS seeds for ``elevation_priority``
    (priority 1 = touches a runway).  Seam-anchored apron vertices are
    HARD but NOT runway, so they are excluded — an apron's priority
    should be hops from the runway, not from a seam."""
    out: set = set()
    for s in layout.shapes:
        if s.role not in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING):
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = _open_ring(list(s.polygon.exterior.coords))
        except _GEOM_EXC:
            continue
        for x, y in coords:
            k = layout.canonical_points.get_or_add(float(x), float(y))
            if k in bucket_to_idx:
                out.add(bucket_to_idx[k])
    return out


def _phase1_hop_priority(n, elev, base_hard, dem_elev,
                         shape_constraints, runway_nodes, coupling=None) -> int:
    """Phase 1 (session 51, user 2026-05-28) — REPLACES the role-tier cascade.

    Algorithm per user spec:
      1. Every pavement shape gets ``elevation_priority`` = hop distance from
         the nearest HARD anchor (runway / seam).  Junctions touching the
         runway = 1; each shape further out = +1.
      2. Iterate shapes by DESCENDING priority (leaves farthest from the
         runway FIRST), tie-break by larger AREA first.
      3. For each shape: seed soft nodes at DEM, then call ``_project_shape``
         to grade-clamp using ONLY the shape's own grade rule.  HARD anchors
         AND vertices already-settled by earlier (higher-priority / leaf-ward)
         shapes are HELD; everything else moves the minimum needed.
      4. Terminal grade rule = flat (single elevation); sloped rects = linear
         along source_axis (≤1.5%); junction/apron = ≤1.5% all-pair Euclidean.

    Result: each shape sits as close to DEM as its own grade rule allows.
    Cross-shape consistency comes from shared-vertex settling: the leaf-ward
    shape settles its vertices first; the runway-ward shape inherits those
    settled vertices via shared-node coincidence.  If a shape cannot satisfy
    its own grade given the held inheritance, Phase 2 relief back-propagates
    outward to redistribute.

    Returns the number of shapes processed (informational).
    """
    if not shape_constraints:
        return 0
    depth = _shape_hop_depth(shape_constraints, runway_nodes)
    # Process by DESCENDING depth (leaves first), tie-break larger area first.
    order_idx = sorted(range(len(shape_constraints)),
                       key=lambda k: (-depth[k], -shape_constraints[k]["area"]))

    # Seed every soft node at DEM (replaces the per-tier yield-to-neighbour
    # seeding).  Held HARD nodes keep their CIFP / DEM-pinned values.
    for i in range(n):
        if not base_hard[i]:
            elev[i] = dem_elev[i]

    settled = list(base_hard)
    for k in order_idx:
        sc = shape_constraints[k]
        held = {i for i in sc["nodes"] if settled[i]}
        _project_shape(elev, sc["nodes"], held, sc["edges"], sc["flat"],
                       coupling)
        for i in sc["nodes"]:
            settled[i] = True
    return len(order_idx)


def _directional_relief(n, elev, is_hard, edge_grade, edge_length,
                        shape_constraints, max_iters, tol_m) -> int:
    """Phase 2: a SHAPE-LEVEL cascade that propagates grade compliance OUTWARD
    from the HARD anchors (runway/seam), building on the cascade in ``elev``
    (NO reseed — the DEM-following surface is never discarded).

    Shapes are processed in order of network distance from the nearest HARD
    anchor.  Each shape is solved as a UNIT (:func:`_project_shape`): the
    vertices it shares with already-settled inward shapes are HELD, and the
    rest move the minimum to keep the WHOLE shape grade-compliant.  So a
    violation is pushed OUTWARD shape by shape until the free terminal/apron
    end absorbs it, and an all-pair surface (apron/junction) flexes as a
    compliant unit instead of shearing.  Outer sweeps reconcile graph cycles.

    Replaces the old symmetric cap projection (which reseeded to DEM and
    over-dropped compliant nodes to the lowest feasible surface).
    """
    if not edge_grade or not shape_constraints:
        return 0
    # BFS rank: network distance from the nearest HARD anchor.
    adj: dict[int, list[tuple[int, float]]] = {}
    for (u, v), _g in edge_grade.items():
        ln = edge_length[(u, v)]
        adj.setdefault(u, []).append((v, ln))
        adj.setdefault(v, []).append((u, ln))
    INF = float("inf")
    rank = [INF] * n
    pq: list[tuple[float, int]] = [(0.0, i) for i in range(n) if is_hard[i]]
    for _d, i in pq:
        rank[i] = 0.0
    heapq.heapify(pq)
    while pq:
        d, u = heapq.heappop(pq)
        if d > rank[u]:
            continue
        for v, ln in adj.get(u, ()):  # type: ignore[arg-type]
            nd = d + ln
            if nd < rank[v]:
                rank[v] = nd
                heapq.heappush(pq, (nd, v))
    comply = max(tol_m, _SPREAD_COMPLY_TOL_M)

    # Shape-HOP depth: number of pavement PIECES between a shape and the
    # runway/seam (BFS over the piece-adjacency graph), NOT metres.  A shape
    # touching a HARD anchor is depth 1; each extra piece in the chain is one
    # level further out on the tree.  Metres-rank is kept only as a tie-break.
    node_owners: dict[int, list[int]] = {}
    for k, sc in enumerate(shape_constraints):
        for i in sc["nodes"]:
            node_owners.setdefault(i, []).append(k)

    def _neighbors(k):
        out = set()
        for i in shape_constraints[k]["nodes"]:
            for k2 in node_owners.get(i, ()):
                if k2 != k:
                    out.add(k2)
        return out

    INF_D = 1 << 30
    depth = [INF_D] * len(shape_constraints)
    dq: deque[int] = deque()
    for k, sc in enumerate(shape_constraints):
        if any(is_hard[i] for i in sc["nodes"]):
            depth[k] = 1
            dq.append(k)
    while dq:
        k = dq.popleft()
        for k2 in _neighbors(k):
            if depth[k2] > depth[k] + 1:
                depth[k2] = depth[k] + 1
                dq.append(k2)
    mrank = [min((rank[i] for i in sc["nodes"]), default=INF)
             for sc in shape_constraints]
    # Process shapes by HOP depth (parents before children), tie-break metres.
    order_idx = sorted(range(len(shape_constraints)),
                       key=lambda k: (depth[k], mrank[k]))
    order = [shape_constraints[k] for k in order_idx]

    # Terminal = RIGID FLAT UNIT (user 2026-05-28).  Each terminal's nodes
    # always share ONE elevation; neighbouring aprons CONFORM to it (hold its
    # vertices, never flex them).  On this reverse pass the terminal may
    # rigid-TRANSLATE (the whole unit, staying flat) the MINIMUM needed to keep
    # its apron connections within grade — anchored at its forward-pass DEM
    # level so the shift is minimal.  This fixes the shared-vertex consensus
    # (the terminal is genuinely flat, not emitted-flat over apron-dragged
    # nodes — the 64 SPJC cross-shape steps) AND lets the runway->terminal
    # relief pull the terminal up/down as the grade demands.
    # COUPLE abutting terminals into ONE rigid flat unit (user: terminal
    # groups must lift as movable COUPLED units).  Two flat pads that share a
    # vertex/edge cannot sit at different levels without a cliff at the shared
    # edge — HECA's south terminal complex (6/7/10) and north (2/9) abut and
    # were each shifted to their OWN DEM centroid, leaving 1.4-3.8 m steps at
    # the shared edges.  Union-find the flat shapes by shared canonical node,
    # so an edge-connected cluster becomes one flat group at one level.
    # (Isolated terminals → singleton cluster → identical to the old per-shape
    # behaviour, so the baseline airports are unchanged.)
    term_scs = [sc for sc in shape_constraints if sc["flat"] and sc["nodes"]]
    _tparent = list(range(len(term_scs)))

    def _tfind(a: int) -> int:
        while _tparent[a] != a:
            _tparent[a] = _tparent[_tparent[a]]
            a = _tparent[a]
        return a

    _node_term: dict[int, int] = {}
    for _ti, _sc in enumerate(term_scs):
        for _i in _sc["nodes"]:
            if _i in _node_term:
                _ra, _rb = _tfind(_ti), _tfind(_node_term[_i])
                if _ra != _rb:
                    _tparent[_ra] = _rb
            else:
                _node_term[_i] = _ti
    _clusters: dict[int, list[int]] = {}
    for _ti in range(len(term_scs)):
        _clusters.setdefault(_tfind(_ti), []).append(_ti)
    terminal_groups: list[set] = []
    for _members in _clusters.values():
        _nodes: set = set()
        for _ti in _members:
            _nodes |= set(term_scs[_ti]["nodes"])
        terminal_groups.append(_nodes)
    terminal_nodes: set = set().union(*terminal_groups) if terminal_groups \
        else set()
    group_of_node: dict[int, int] = {}
    for gi, g in enumerate(terminal_groups):
        for i in g:
            group_of_node[i] = gi

    # Rigid level coupling: a rect's two flat-end cross-corner pairs each move
    # as ONE level (user 2026-05-28), so whichever neighbour drags an end keeps
    # the shared edge flat.  The coupling map moves a whole group together
    # whenever any member moves, and a group is held iff any member is held.
    coupling = _build_level_coupling(shape_constraints)
    # ENFORCE the equality (not merely react to it): the cap projection only
    # equalises a group WHEN a move is triggered, so a rect end that Phase 1
    # left un-level (its two corners followed DEM independently) would stay
    # tilted and the emit collapse would then disagree with the neighbour.
    # Level every coupled group to its mean ONCE up front — a flat cross-end
    # MUST be level (mandatory geometry, not grade-smearing).  Skip groups
    # pinned by a HARD anchor or a terminal (those conform via their own path).
    for members in {tuple(sorted(v)) for v in coupling.values()}:
        if any(is_hard[m] for m in members):
            continue
        if terminal_nodes.intersection(members):
            continue
        lvl = sum(elev[m] for m in members) / len(members)
        for m in members:
            elev[m] = lvl

    # ONE reverse pass (user 2026-05-28): runway/seam -> leaves, a SINGLE
    # outward traversal — NOT an iterative relaxation.  Process shapes by
    # ascending distance from the HARD anchors; HOLD each shape's already-
    # settled (runway-ward) vertices and FORCE its outward vertices to grade
    # compliance, so the slack travels strictly OUTWARD and the free leaves /
    # terminals absorb the final adjustment.  Because a settled shape's
    # vertices are held by every more-outward shape and never move back, the
    # violation only propagates outward and is resolved in this one pass — the
    # forward Phase-1 pass and this reverse pass are the only two the model
    # needs (the old 60-sweep relaxation fought itself and never converged).
    settled = list(is_hard)
    for k, sc in enumerate(order):
        gi = group_of_node.get(sc["nodes"][0]) if sc["nodes"] else None
        if gi is not None and set(sc["nodes"]) <= terminal_groups[gi]:
            # Terminal is held at its pre-solve taxi-route SEED (the grade-feasible
            # level w.r.t. every adjacent runway).  Do NOT rigid-shift it toward
            # its local apron boundary — that ratchets it UP toward the high side
            # and under-slopes the apron (user 2026-06-09); the apron must grade
            # DOWN to the seeded pad, and any residual the runway-flex absorbs.
            pass
        else:
            held = {i for i in sc["nodes"] if settled[i]}
            held |= terminal_nodes.intersection(sc["nodes"])  # conform to terminals
            _project_shape(elev, sc["nodes"], held, sc["edges"], sc["flat"],
                           coupling)
        for i in sc["nodes"]:
            settled[i] = True

    # Direct difference-constraint convergence (user 2026-05-28): the
    # directional passes leave residual at cycle-closing edges (two runway-paths
    # converging on a shape at incompatible elevations).  Compute each node's
    # feasible band from the HARD anchors via shortest cap-paths, then
    # cap-project all within-shape edges CLAMPED to those bands until grade-
    # compliant.  Bands handle the multi-path structure natively and bound the
    # iteration so it converges; genuinely infeasible seam nodes (lo>hi, the
    # terrain-dictated seam cross-slope) are left at their band midpoint and
    # their both-HARD edges are skipped.
    n = len(elev)
    all_edges = [e for sc in shape_constraints for e in sc["edges"]]
    lo, hi = _grade_bands(n, elev, is_hard, all_edges)
    # Hold terminals fixed so building pads stay at their DEM level; the
    # difference-constraint solve grades the aprons/junctions to that level.
    n_sweeps, _viol = _project_within_bands(
        elev, all_edges, is_hard, lo, hi, coupling,
        held_extra=terminal_nodes,
        max_sweeps=1000,
        tol=max(tol_m, _SPREAD_COMPLY_TOL_M))
    # Terminal level is now set by the pre-solve taxi-route SEED
    # (``_seed_terminals_from_taxi_routes``, user 2026-06-09), which places each
    # coupled pad at its grade-feasible level w.r.t. every adjacent runway so the
    # apron can slope DOWN to the runway.  The old alternating yield
    # (``_yield_terminals_alternating``) RATCHETED terminals UP toward high held
    # neighbours (HECA terminal7 71.9 -> 76.4), under-sloping the apron and
    # forcing the runway to over-flex up to meet it — so it is REPLACED by the
    # seed, not run on top of it.
    return 1 + n_sweeps


# Tolerance buffer for the in-pavement visibility test: a chord is "visible"
# (a real grade constraint) if it stays within the polygon grown by this margin.
# Absorbs polygon-edge-coincident chords + float noise; far smaller than any real
# apron void, so it never bridges a genuine gap between pavement arms.
_GRADE_VISIBILITY_BUFFER_M = 1.0


def _visible_grade_edges(coords, idx, cap, polygon, container=None,
                         max_len=None):
    """All-pair grade edges restricted to MUTUALLY-VISIBLE vertices — the chord
    between the two vertices stays inside ``polygon`` (grown by
    ``_GRADE_VISIBILITY_BUFFER_M``).  This is the in-pavement visibility graph:
    the band Dijkstra over these edges yields the true geodesic distance, so a
    non-convex apron's far ends are correctly far apart instead of joined by a
    Euclidean chord that cuts across non-pavement.  Falls back to plain all-pair
    if the geometry op fails (degenerate/invalid polygon).

    ``container``: optional PREPARED geometry to test chords against instead
    of the shape's own buffered polygon.  Junctions pass the airside-pavement
    UNION: a chord that leaves the junction across a NEIGHBOUR's pavement is a
    physically real grade path (the s73 #192 lesson — dropping it let the
    junction step 0.66 m off the rect edge it hugs), while a chord across a
    true void (grass between arms) stays excluded.

    ``max_len`` (the ROUTE-FIELD LOCAL WINDOW, user-approved s73-p3 #3):
    visibility chords are a LOCAL smoothness law only — pairs farther apart
    than this are NOT graded against each other; the long-range law is the
    taxi-route band (``_runway_reach_bands``).  km-scale chords (and chains
    of them across shared nodes) systematically UNDER-measure the real taxi
    route and manufacture infeasibility (HECA s73-p10g: 2.5 km chord chain
    vs 3.08 km route = 8.5 m false demand).  RING-ADJACENT pairs always
    survive regardless of length — the physical edge X-Plane lerps."""
    from shapely.geometry import LineString
    m = len(idx)
    try:
        if container is not None:
            _vis = container.contains
        else:
            from shapely.prepared import prep
            pg = prep(polygon.buffer(_GRADE_VISIBILITY_BUFFER_M))
            _vis = pg.contains
    except _GEOM_EXC:
        _vis = None
    out: list[tuple[int, int, float]] = []
    for a in range(m):
        if idx[a] is None:
            continue
        xa, ya = coords[a]
        for b in range(a + 1, m):
            if idx[b] is None or idx[a] == idx[b]:
                continue
            xb, yb = coords[b]
            d = math.hypot(xa - xb, ya - yb)
            if d < 0.5:
                continue
            if max_len is not None and d > max_len \
                    and not (b == a + 1 or (a == 0 and b == m - 1)):
                continue          # beyond the local window, not a ring edge
            if _vis is not None and not (NETWORK_PROFILE_MODEL
                                         and d <= 20.0):
                # short same-shape pairs are unconditional under the
                # network model: the validator re-tests visibility on the
                # EMITTED polygon, and sub-20 m chords flutter across the
                # two geometries (SPJC #92: a 4.5 m pair the solver
                # dropped and the validator kept = an unprojected 0.7 m
                # step at a smoothing-zone boundary)
                try:
                    if not _vis(LineString(((xa, ya), (xb, yb)))):
                        continue
                except _GEOM_EXC:
                    pass
            out.append((idx[a], idx[b], cap * d))
    return out


def _build_shape_constraints(layout, bucket_to_idx):
    """Per-shape grade constraints for the directional relief: one entry per
    soft pavement shape with ``{nodes, edges, flat}`` — its node indices, its
    OWN internal grade edges ``(i, j, cap_m)``, and whether it must stay flat
    (terminal).  Rects use flat-cross (cap≈0) + axial edges; aprons use the
    in-pavement VISIBILITY graph (geodesic, see ``_visible_grade_edges``);
    junction/seam-rect use all-pair; terminal is flat.  Runway/seam are HARD,
    not included."""
    out = []
    # Airside-pavement union, prepared, for JUNCTION chord-visibility (see
    # ``_visible_grade_edges``): junction chords may cross neighbouring
    # pavement (real grade paths) but not true voids.  Built once per solve.
    airside_buf = None
    try:
        from shapely.ops import unary_union
        from shapely.prepared import prep
        polys = [s.polygon for s in layout.shapes
                 if s.role in PAVEMENT_ROLES
                 and s.polygon is not None and not s.polygon.is_empty]
        if polys:
            airside_buf = prep(
                unary_union(polys).buffer(_GRADE_VISIBILITY_BUFFER_M))
    except _GEOM_EXC:
        airside_buf = None
    for s in layout.shapes:
        if s.role not in PAVEMENT_ROLES or s.role == ROLE_RUNWAY:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        coords = _open_ring(list(s.polygon.exterior.coords))
        if len(coords) < 2:
            continue
        idx = [bucket_to_idx.get(
            layout.canonical_points.get_or_add(float(x), float(y)))
            for x, y in coords]
        nodes = [i for i in idx if i is not None]
        if len(nodes) < 2:
            continue
        cap = _role_grade(s.role)
        # Terminal pads: with ``config.TERMINAL_PADS_SLOPE`` (evaluation state,
        # user 2026-06-10) EVERY pad may slope up to the terminal cap through
        # the visibility graph, like an apron — the route-justified runway
        # profiles (05C 110.9, not the rejected 104.4 over-dip) leave chain
        # tension only the terminals can drain.  With it False, pads are rigid
        # FLAT by default (flatness preferred — the config cap is the MAX a
        # terminal MAY slope, not a mandate) and only a pad the taxi-route
        # seed marked SQUEEZED (straddles a low and a high runway, cannot be
        # one level in grade to both) grades at the cap (user 2026-06-09:
        # flatness yields to grade, but ONLY where grade demands it).
        if s.role == ROLE_TERMINAL and not TERMINAL_PADS_SLOPE:
            _sloped = getattr(layout, "_sloped_terminal_nodes", None)
            if not (_sloped and any(i in _sloped for i in nodes)):
                cap = 0.0
        flat = (cap <= 0.0)
        edges: list[tuple[int, int, float]] = []
        flat_pairs: list[tuple[int, int]] = []   # rect flat-end coupled pairs
        # A clean PLANAR rect (altitude_high/low) gets the flat-cross + axial
        # constraints.  A per-vertex ``node_altitudes`` piece is NOT planar —
        # e.g. the tile-cut seam WEDGE that follows the seam terrain's cross-
        # slope and blends back to the rect (user 2026-05-28) — so it must use
        # the all-pair rule, NOT a cap-0 flat end (which would force its two
        # seam-pinned corners equal and read as a spurious 3.3 m violation).
        is_rect = s.role in SLOPING_RECT_ROLES and len(coords) == 4 \
            and all(i is not None for i in idx) \
            and s.node_altitudes is None
        if flat:
            pass                                  # handled by _project_shape
        elif is_rect:
            # Identify the two AXIS-END (flat-cross, cap 0) edges and the two
            # AXIAL (sloping, cap = grade·length) edges by projecting each ring
            # edge onto ``source_axis`` (user 2026-05-28) — NOT by ring index:
            # absorption / snaps / tile-cut can rotate the ring, and a stale
            # [H,L,L,H] index assumption mis-labels which edges must stay flat.
            ring_edges = [(0, 1), (1, 2), (2, 3), (3, 0)]
            scored = []
            adx = ady = None
            ax = getattr(s, "source_axis", None)
            if ax is not None and not ax.is_empty:
                acs = list(ax.coords)
                if len(acs) >= 2:
                    adx, ady = acs[-1][0] - acs[0][0], acs[-1][1] - acs[0][1]
                    al = math.hypot(adx, ady) or 1.0
                    adx, ady = adx / al, ady / al
            for (a, b) in ring_edges:
                ex, ey = coords[b][0] - coords[a][0], coords[b][1] - coords[a][1]
                el = math.hypot(ex, ey) or 1e-9
                # |edge·axis|/|edge|: 1 = axial (sloping), 0 = perpendicular (flat)
                par = abs(ex * adx + ey * ady) / el if adx is not None else 0.0
                scored.append((par, a, b, el))
            scored.sort()                # ascending: first 2 = flat, last 2 = axial
            for n, (_par, a, b, el) in enumerate(scored):
                if idx[a] is None or idx[b] is None or idx[a] == idx[b]:
                    continue
                if n < 2:                # the two most-perpendicular = flat ends
                    edges.append((idx[a], idx[b], 0.0))
                    flat_pairs.append((idx[a], idx[b]))
                else:                    # the two most-parallel = sloping edges
                    edges.append((idx[a], idx[b], cap * el))
        elif s.role in (ROLE_APRON, ROLE_TERMINAL, ROLE_JUNCTION):
            # In-pavement VISIBILITY graph for APRONS, GRADED terminals (when
            # TERMINAL_MAX_GRADE > 0 — large near-flat pads, same as an apron)
            # and JUNCTIONS.  The within-shape grade
            # limit applies ALONG the pavement, so a grade edge is added only
            # between MUTUALLY-VISIBLE vertices (the chord stays inside the
            # polygon).  On a non-convex shape the Euclidean chord between two
            # far vertices leaves the polygon and cuts across non-pavement,
            # fabricating a phantom short grade path; restricting to visible
            # pairs makes the band Dijkstra compute the true GEODESIC distance
            # (visibility-graph shortest path = exact geodesic in a simple
            # polygon — bends at reflex vertices, all of which are nodes here).
            # Convex shapes: every pair visible, so identical to all-pair.
            # JUNCTIONS added s73 (user 2026-06-10): the old "small and
            # near-convex" assumption fails for long-armed junctions — HECA
            # #291 (149×229 m, solidity 0.82) carried 71/228 all-pair chords
            # OUTSIDE its polygon, and its 6 tightest constraints were all
            # fictitious cross-arm chords, pinning it near-flat so the
            # taxiway-T grade piled into the next rect (#75 at 4.1 %) instead
            # of flowing through.  check_grade has visibility-gated junctions
            # since s62 — this aligns the solver with the validator.
            # Junctions test chords against the AIRSIDE UNION, not their own
            # polygon: a junction hugs its rects, so its cross-notch chords
            # run over neighbouring pavement = real grade paths (#192 stepped
            # 0.66 m off TX29's edge when those were dropped); only chords
            # over true voids are excluded.
            # ROUTE-FIELD MODEL: visibility chords are demoted to a LOCAL
            # smoothness window; the long-range law is the taxi-route band
            # in the enforce (docs/route_field_model.md §3).  Ring-adjacent
            # pairs always survive inside _visible_grade_edges.
            vis_edges = _visible_grade_edges(
                coords, idx, cap, s.polygon,
                container=(airside_buf if s.role == ROLE_JUNCTION
                           else None),
                max_len=(ROUTE_FIELD_LOCAL_WINDOW_M if ROUTE_FIELD_MODEL
                         else None))
            # PER-AXIS JUNCTION GRADING (user 2026-06-10): the 1.5 % cap
            # applies along the taxi CENTERLINE.  A chord between two
            # vertices following the same (curved) axis caps at the
            # ARC length between their projections — a straight chord
            # under-measures a turning route and pins the junction flat
            # (HECA #282: A4's mouth sat 47 m by chord from a runway
            # vertex but ~120 m along the curved exit centerline, so the
            # whole 1.8 m climb the exit should carry was forbidden).
            # Cross-axis diagonals are an unregulated direction (ICAO
            # Annex 14 §3.9 / EASA CS-ADR-DSN.D.265/.280 regulate
            # longitudinal-along-route + transverse) and are dropped;
            # ring-adjacent pairs always survive (the physical edge).
            if s.role == ROLE_JUNCTION and _PER_AXIS_JUNCTIONS:
                axes = _collect_junction_axes(layout, s.polygon)
                if axes:
                    from shapely.geometry import Point as _Pt
                    m2 = len(coords)
                    ring_adj = set()
                    pos2: dict = {}
                    for a2 in range(m2):
                        ia2 = idx[a2]
                        ib2 = idx[(a2 + 1) % m2]
                        if ia2 is not None:
                            pos2.setdefault(ia2, coords[a2])
                        if ia2 is not None and ib2 is not None:
                            ring_adj.add((min(ia2, ib2), max(ia2, ib2)))
                    pdist: dict = {}
                    kept: list = []
                    for (ea, eb, ecap) in vis_edges:
                        pa, pb = pos2.get(ea), pos2.get(eb)
                        if pa is None or pb is None:
                            kept.append((ea, eb, ecap))
                            continue
                        arc_best = None
                        for ax2 in axes:
                            ka2 = (id(ax2), ea)
                            kb2 = (id(ax2), eb)
                            da2 = pdist.get(ka2)
                            if da2 is None:
                                da2 = ax2.distance(_Pt(pa))
                                pdist[ka2] = da2
                            db2 = pdist.get(kb2)
                            if db2 is None:
                                db2 = ax2.distance(_Pt(pb))
                                pdist[kb2] = db2
                            if (da2 <= JUNCTION_AXIS_PERP_TOL_M
                                    and db2 <= JUNCTION_AXIS_PERP_TOL_M):
                                arc = abs(ax2.project(_Pt(pa))
                                          - ax2.project(_Pt(pb)))
                                if arc_best is None or arc > arc_best:
                                    arc_best = arc
                        if arc_best is not None:
                            kept.append((ea, eb,
                                         max(ecap, cap * arc_best)))
                        elif (min(ea, eb), max(ea, eb)) in ring_adj:
                            kept.append((ea, eb, ecap))
                        # else: cross-axis diagonal — dropped
                    vis_edges = kept
            edges.extend(vis_edges)
        else:
            # All-pair (seam-cut rect / service junction): small near-convex
            # shapes.
            m = len(idx)
            for a in range(m):
                if idx[a] is None:
                    continue
                for b in range(a + 1, m):
                    if idx[b] is None or idx[a] == idx[b]:
                        continue
                    d = math.hypot(coords[a][0] - coords[b][0],
                                   coords[a][1] - coords[b][1])
                    if d >= 0.5:
                        edges.append((idx[a], idx[b], cap * d))
        out.append({"nodes": nodes, "edges": edges, "flat": flat,
                    "flat_pairs": flat_pairs,
                    "area": float(s.polygon.area),
                    "role": s.role,
                    "ref": s.ref or ""})
    return out


def _principal_axis(pts):
    """Unit direction of a point set's longest extent (the farthest-apart
    pair).  Runways are long & thin, so the extreme pair defines the axis.
    ``pts`` is small (runway corners), so the O(n^2) scan is fine."""
    best_d2 = -1.0
    best = None
    for i in range(len(pts)):
        xi, yi = pts[i]
        for j in range(i + 1, len(pts)):
            dx = pts[j][0] - xi
            dy = pts[j][1] - yi
            d2 = dx * dx + dy * dy
            if d2 > best_d2:
                best_d2 = d2
                best = (dx, dy)
    if best is None:
        return None
    dx, dy = best
    ln = math.hypot(dx, dy) or 1.0
    return (dx / ln, dy / ln)


def _runway_threshold_nodes(layout, bucket_to_idx) -> set:
    """Node idxs at the extreme axial ENDS of each runway — the CIFP
    thresholds (user 2026-05-28, runway-flex pass).  Group ROLE_RUNWAY
    sub-rects by designator, fit the runway axis, and return the corner nodes
    whose axial projection is within ``_RUNWAY_FLEX_THRESHOLD_TOL_M`` of either
    extreme.  Both corners of a runway-end cross-edge share the extreme
    projection (the end edge is perpendicular to the axis), and the next
    cross-section is a full sub-rect inward, so a tight tolerance captures the
    end pair and nothing else.  These stay HARD when the interior softens."""
    by_ref: dict[str, list] = {}
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        by_ref.setdefault(s.ref or "", []).append(s)
    out: set = set()
    for shapes in by_ref.values():
        pts: list[tuple[float, float, int]] = []
        for s in shapes:
            for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
                k = layout.canonical_points.get_or_add(float(x), float(y))
                idx = bucket_to_idx.get(k)
                if idx is not None:
                    pts.append((x, y, idx))
        if len(pts) < 2:
            continue
        ax = _principal_axis([(x, y) for (x, y, _) in pts])
        if ax is None:
            continue
        adx, ady = ax
        proj = [(x * adx + y * ady, idx) for (x, y, idx) in pts]
        pmin = min(p for p, _ in proj)
        pmax = max(p for p, _ in proj)
        tol = _RUNWAY_FLEX_THRESHOLD_TOL_M
        for p, idx in proj:
            if p - pmin <= tol or pmax - p <= tol:
                out.add(idx)
    return out


def _build_runway_constraints(layout, bucket_to_idx):
    """Per-shape grade constraints for the RUNWAY chain (user 2026-05-28,
    runway-flex pass) — one entry per ROLE_RUNWAY / ROLE_RUNWAY_CROSSING shape,
    mirroring :func:`_build_shape_constraints` but for runways (which are
    otherwise excluded as pure HARD anchors).  Runway rects have NO
    ``source_axis``, so the two SHORT ring edges are the flat cross-ends (cap 0,
    coupled so the cross-section can't tilt) and the two LONG edges are axial
    (cap = runway grade x length).  Runway crossings are irregular
    ``node_altitudes`` polygons -> all-pair, like a junction.  Consecutive
    sub-rects share their cross-edge corners, so these entries form one
    connected threshold->interior->threshold grade chain."""
    # Per-ref runway axis (origin, unit dir, length) so an axial edge in the
    # first/last RUNWAY_END_FRACTION gets the tighter RUNWAY_END_GRADE (0.8%)
    # rather than the uniform 1.5%.  Without this, a runway-flex MOVE regrades
    # the chain at a flat 1.5% and the end zones silently exceed the EASA/ICAO
    # end-grade rule (HECA 05C/23C: valley flanks pulled to 1.5% inside the
    # first/last quarter).  The move must keep the WHOLE chain compliant.
    rwy_axis: dict = {}
    _by_ref: dict = {}
    for s in layout.shapes:
        if (s.role == ROLE_RUNWAY and (s.ref or "")
                and s.polygon is not None and not s.polygon.is_empty):
            _by_ref.setdefault(s.ref, []).append(s)
    for ref, ss in _by_ref.items():
        pts = [p for s in ss
               for p in _open_ring(list(s.polygon.exterior.coords))]
        best = -1.0
        A = B = None
        for ai in range(len(pts)):
            xa, ya = pts[ai]
            for bi in range(ai + 1, len(pts)):
                d2 = (pts[bi][0] - xa) ** 2 + (pts[bi][1] - ya) ** 2
                if d2 > best:
                    best, A, B = d2, pts[ai], pts[bi]
        if A is not None and best > 0:
            ln = math.sqrt(best)
            rwy_axis[ref] = (A[0], A[1],
                             (B[0] - A[0]) / ln, (B[1] - A[1]) / ln, ln)

    out = []
    for s in layout.shapes:
        if s.role not in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING):
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        coords = _open_ring(list(s.polygon.exterior.coords))
        if len(coords) < 2:
            continue
        idx = [bucket_to_idx.get(
            layout.canonical_points.get_or_add(float(x), float(y)))
            for x, y in coords]
        nodes = [i for i in idx if i is not None]
        if len(nodes) < 2:
            continue
        cap = _role_grade(s.role)
        _ax = rwy_axis.get(s.ref or "") if s.role == ROLE_RUNWAY else None

        def _axial_cap(a_xy, b_xy):
            """Grade cap for an axial edge: RUNWAY_END_GRADE inside the
            first/last quarter, else the uniform runway cap."""
            if _ax is None:
                return cap
            ox, oy, ux, uy, ln = _ax
            mx = 0.5 * (a_xy[0] + b_xy[0])
            my = 0.5 * (a_xy[1] + b_xy[1])
            frac = (((mx - ox) * ux + (my - oy) * uy) / ln) if ln > 0 else 0.5
            if frac < RUNWAY_END_FRACTION or frac > 1.0 - RUNWAY_END_FRACTION:
                return RUNWAY_END_GRADE
            return cap

        edges: list[tuple[int, int, float]] = []
        flat_pairs: list[tuple[int, int]] = []
        is_rect = (s.role == ROLE_RUNWAY and len(coords) == 4
                   and all(i is not None for i in idx)
                   and s.node_altitudes is None)
        if is_rect:
            ring_edges = [(0, 1), (1, 2), (2, 3), (3, 0)]
            scored = []
            for (a, b) in ring_edges:
                el = math.hypot(coords[b][0] - coords[a][0],
                                coords[b][1] - coords[a][1]) or 1e-9
                scored.append((el, a, b))
            scored.sort()                # ascending: first 2 short, last 2 long
            for k, (el, a, b) in enumerate(scored):
                if idx[a] is None or idx[b] is None or idx[a] == idx[b]:
                    continue
                if k < 2:                # the two SHORT edges = flat cross-ends
                    edges.append((idx[a], idx[b], 0.0))
                    flat_pairs.append((idx[a], idx[b]))
                else:                    # the two LONG edges = sloping axial
                    edges.append((idx[a], idx[b],
                                  _axial_cap(coords[a], coords[b]) * el))
        else:
            m = len(idx)
            for a in range(m):
                if idx[a] is None:
                    continue
                for b in range(a + 1, m):
                    if idx[b] is None or idx[a] == idx[b]:
                        continue
                    d = math.hypot(coords[a][0] - coords[b][0],
                                   coords[a][1] - coords[b][1])
                    if d >= 0.5:
                        edges.append((idx[a], idx[b], cap * d))
        out.append({"nodes": nodes, "edges": edges, "flat": False,
                    "flat_pairs": flat_pairs,
                    "area": float(s.polygon.area),
                    "role": s.role, "ref": s.ref or ""})
    return out


def _max_within_excess(elev, edges) -> float:
    """Worst grade excess (|de| - cap, metres) over ``edges`` — the metric the
    within-shape grade test sees.  No hard/soft skipping: a violation counts
    wherever it lands."""
    mx = 0.0
    for (i, j, cap) in edges:
        ex = abs(elev[i] - elev[j]) - cap
        if ex > mx:
            mx = ex
    return mx


def _within_excess_stats(elev, edges, comply) -> tuple[float, int, float]:
    """``(worst, count, total)`` grade excess over ``edges`` counting only edges
    over ``comply``.  A runway/threshold yield is accepted when it reduces the
    violation COUNT/TOTAL without worsening the worst — a single stubborn
    violation elsewhere (an unrelated junction) must not veto a real fix that
    clears a different edge."""
    worst = 0.0
    count = 0
    total = 0.0
    for (i, j, cap) in edges:
        ex = abs(elev[i] - elev[j]) - cap
        if ex > comply:
            count += 1
            total += ex
            if ex > worst:
                worst = ex
    return worst, count, total


def _seam_pinned_runway_nodes(layout, bucket_to_idx) -> set:
    """Runway / runway-crossing node idxs that coincide with a tile-boundary
    seam anchor.  The seam is the TOP truth (terrain mesh is pinned to raw HGT
    at the boundary), so these stay HARD even when the rest of the runway is
    released to yield to a seam (user 2026-05-28)."""
    seam_keys = getattr(layout, "_seam_anchor_keys", None) or set()
    if not seam_keys:
        return set()
    from ..layout import SHARED_VERTEX_TOL_M
    bk_s = 1.0 / SHARED_VERTEX_TOL_M
    out: set = set()
    for s in layout.shapes:
        if s.role not in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING):
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            if (int(round(x * bk_s)), int(round(y * bk_s))) not in seam_keys:
                continue
            idx = bucket_to_idx.get(
                layout.canonical_points.get_or_add(float(x), float(y)))
            if idx is not None:
                out.add(idx)
    return out


def _runway_crossing_nodes(layout, bucket_to_idx) -> set:
    """Node idxs where two runways cross — every node of a ROLE_RUNWAY_CROSSING
    shape (the intersection pavement) plus any node shared by two different
    runway refs.  A crossing point is ONE canonical node belonging to BOTH
    runways, so its elevation must stay consistent for both: it is held in the
    re-smooth so flexing one runway can't drag the crossing off the other (CYXY
    02/20 × 14R/32L).  Parallel-runway airports (HECA, SPJC) have none."""
    out: set = set()
    rwy_ref_of: dict = {}               # node idx -> set of runway refs using it
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
            continue
        is_crossing = s.role == ROLE_RUNWAY_CROSSING
        is_runway = s.role == ROLE_RUNWAY
        if not (is_crossing or is_runway):
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            idx = bucket_to_idx.get(
                layout.canonical_points.get_or_add(float(x), float(y)))
            if idx is None:
                continue
            if is_crossing:
                out.add(idx)
            else:
                rwy_ref_of.setdefault(idx, set()).add(s.ref or "")
    for idx, refs in rwy_ref_of.items():
        if len(refs) >= 2:              # node shared by two distinct runways
            out.add(idx)
    return out


def _runway_profile_compliance(layout, elev, bucket_to_idx):
    """``(worst |grade|, curvature-violation count)`` over every runway's
    centerline profile reconstructed from the CURRENT ``elev`` array — the
    metric the runway-flex guard holds to (longitudinal grade + FAA
    vertical-curve rate-of-grade-change).  A runway is a chain of 4-corner
    ``ROLE_RUNWAY`` rects sharing flat cross-ends; sample one elevation per
    cross-end along the per-ref axis (longest vertex pair), then measure grade
    per segment and grade-change between consecutive segments against
    ``RUNWAY_MAX_GRADE_CHANGE_PER_M``."""
    from auto_patch.config import (RUNWAY_MAX_GRADE_CHANGE_PER_M,)
    NOISE = 0.05
    by_ref: dict = {}
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY or s.polygon is None or s.polygon.is_empty:
            continue
        cs = _open_ring(list(s.polygon.exterior.coords))
        if len(cs) != 4:
            continue
        by_ref.setdefault(s.ref or "", []).append(cs)
    worst_g = 0.0
    n_curv = 0
    for rects in by_ref.values():
        pts = [p for cs in rects for p in cs]
        best = -1.0
        A = B = None
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                d2 = ((pts[j][0] - pts[i][0]) ** 2
                      + (pts[j][1] - pts[i][1]) ** 2)
                if d2 > best:
                    best, A, B = d2, pts[i], pts[j]
        if A is None or best <= 0:
            continue
        L = math.sqrt(best)
        ux, uy = (B[0] - A[0]) / L, (B[1] - A[1]) / L
        samples = []
        for cs in rects:
            es = []
            ok = True
            for (x, y) in cs:
                idx = bucket_to_idx.get(
                    layout.canonical_points.get_or_add(float(x), float(y)))
                if idx is None:
                    ok = False
                    break
                es.append(elev[idx])
            if not ok:
                continue
            edges = [(0, 1), (1, 2), (2, 3), (3, 0)]
            edges.sort(key=lambda ab: math.hypot(
                cs[ab[1]][0] - cs[ab[0]][0], cs[ab[1]][1] - cs[ab[0]][1]))
            for (a, b) in edges[:2]:           # the two short edges = cross-ends
                mx, my = 0.5 * (cs[a][0] + cs[b][0]), 0.5 * (cs[a][1] + cs[b][1])
                samples.append(((mx - A[0]) * ux + (my - A[1]) * uy,
                                0.5 * (es[a] + es[b])))
        if len(samples) < 2:
            continue
        samples.sort(key=lambda t: t[0])
        merged = []
        for d, e in samples:
            if merged and abs(d - merged[-1][0]) <= 5.0:
                pd, pe, pn = merged[-1]
                k = pn + 1
                merged[-1] = ((pd * pn + d) / k, (pe * pn + e) / k, k)
            else:
                merged.append((d, e, 1))
        grades = []
        for i in range(len(merged) - 1):
            seg = merged[i + 1][0] - merged[i][0]
            if seg < 0.5:
                continue
            g = (merged[i + 1][1] - merged[i][1]) / seg
            worst_g = max(worst_g, abs(g))
            grades.append((g, seg))
        for i in range(len(grades) - 1):
            gl, Ll = grades[i]
            gr, Lr = grades[i + 1]
            max_dg = RUNWAY_MAX_GRADE_CHANGE_PER_M * 0.5 * (Ll + Lr)
            if abs(gr - gl) - max_dg > NOISE * (1.0 / Ll + 1.0 / Lr):
                n_curv += 1
    return worst_g, n_curv


def _runway_centerline_chain(layout, bucket_to_idx, rects):
    """Ordered centerline chain for ONE runway (its 4-corner ``rects``): a list
    of ``{"d": axis-distance, "idxs": {node indices at this cross-position}}``
    sorted along the per-ref axis (longest vertex pair), plus the axis length.
    Two abutting rects share a cross-edge → its two corners fall in the same
    ~2 m bucket → one chain position carrying both rects' shared nodes.  Returns
    ``(positions, axis_length)`` or ``(None, 0)``."""
    pts = [p for cs in rects for p in cs]
    best = -1.0
    A = B = None
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d2 = (pts[j][0] - pts[i][0]) ** 2 + (pts[j][1] - pts[i][1]) ** 2
            if d2 > best:
                best, A, B = d2, pts[i], pts[j]
    if A is None or best <= 0:
        return None, 0.0
    L = math.sqrt(best)
    ux, uy = (B[0] - A[0]) / L, (B[1] - A[1]) / L
    pos_map: dict = {}
    for cs in rects:
        edges = [(0, 1), (1, 2), (2, 3), (3, 0)]
        edges.sort(key=lambda ab: math.hypot(
            cs[ab[1]][0] - cs[ab[0]][0], cs[ab[1]][1] - cs[ab[0]][1]))
        for (a, b) in edges[:2]:                 # the two short edges = cross-ends
            for (x, y) in (cs[a], cs[b]):
                idx = bucket_to_idx.get(
                    layout.canonical_points.get_or_add(float(x), float(y)))
                if idx is None:
                    continue
                d = (x - A[0]) * ux + (y - A[1]) * uy
                e = pos_map.setdefault(round(d / 2.0), {"idxs": set(), "d": d})
                e["idxs"].add(idx)
    positions = sorted(pos_map.values(), key=lambda e: e["d"])
    return positions, L


def _resmooth_runways_in_elev(layout, elev, bucket_to_idx, anchored_nodes,
                              demand_lo=None,
                              demand_hi=None, only_refs=None):
    """Re-fit every runway's centerline elevations IN ``elev`` to an FAA grade +
    vertical-curve compliant profile (``runway_segments.faa_joint_solve``),
    anchored at ``anchored_nodes`` (thresholds + seam-pinned + crossings).
    Used after the runway-flex band solve, which is piecewise-linear and leaves
    kinks at the flex boundaries: re-smoothing turns a benign flatten (CYXY) into
    a genuinely compliant curve.  Under the demand synthesis,
    ``demand_lo``/``demand_hi`` carry per-node flex bounds: the envelope +
    consistency filters prune jointly-infeasible bounds and the iterative
    anchor loop grades ONE smooth profile threshold→demand→threshold so a
    connecting taxiway can pull the runway middle down/up WITHOUT moving the
    thresholds, instead of the solve filling the dip back toward terrain.
    ``only_refs`` restricts to those runway refs (sequential per-ref commits).
    Mutates ``elev``; operates on the FULL runway centerline as the 1-D path."""
    from auto_patch.pavement.runway_segments import faa_joint_solve
    by_ref: dict = {}
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY or s.polygon is None or s.polygon.is_empty:
            continue
        cs = _open_ring(list(s.polygon.exterior.coords))
        if len(cs) == 4:
            by_ref.setdefault(s.ref or "", []).append(cs)
    for ref_key, rects in by_ref.items():
        if only_refs is not None and ref_key not in only_refs:
            continue
        positions, L = _runway_centerline_chain(layout, bucket_to_idx, rects)
        if not positions or len(positions) < 3 or L <= 0:
            continue
        if demand_lo is not None:
            # MERGE sub-5 m stations (junction-cut slivers — the same
            # class check_runway_profile merges): with interior demand
            # anchors in play, ``faa_rate_of_change_pass`` computes
            # grades over the 2 m pair and its anchored-neighbour
            # displacement spirals (measured: stations at d=1099/1101
            # solved to 28.85 between 71/67 anchors).  Demand-path only,
            # so the gate-off profile is untouched.
            mpos: list = []
            for p in positions:
                if mpos and p["d"] - mpos[-1]["d"] < 5.0:
                    prev = mpos[-1]
                    prev["idxs"] = list(prev["idxs"]) + list(p["idxs"])
                    prev["d"] = (prev["d"] + p["d"]) / 2.0
                else:
                    mpos.append(dict(p))
            if len(mpos) >= 3:
                positions = mpos
        fractions = [p["d"] / L for p in positions]
        elevs = [sum(elev[i] for i in p["idxs"]) / len(p["idxs"])
                 for p in positions]
        anchored = [any(i in anchored_nodes for i in p["idxs"])
                    for p in positions]
        if not any(anchored):
            anchored[0] = anchored[-1] = True       # fallback: pin the ends
        # NO flat-seed: preserve the runway's settled (incoming) profile and let
        # ``faa_joint_solve`` move it the MINIMUM needed to stay grade+curvature
        # compliant through the anchors (thresholds + seam + route-band binding +
        # runway-crossing nodes).  Flat-seeding here would flatten the WHOLE
        # runway toward the threshold line — maximal movement, not minimal — which
        # pulls it off features graded to its original profile (SPJC's
        # edge-sharing junction, CYXY's crossing runway).  Minimum flex = move
        # only where a route anchor (or crossing) requires it.
        #
        # DEMAND-BOUND ITERATION (2026-06-09): a single demand anchor is
        # not enough — away from it, the smoothed profile can rise above
        # OTHER stations' terminal-chain ceilings (or drop below their
        # floors), leaving pavement that cannot reach the runway (HECA:
        # 3.1 m worst at runway-adjacent junctions).  With per-station
        # bounds (``demand_lo``/``demand_hi``, the held-terminal band):
        # solve, find the worst bound violation, anchor that station AT
        # its bound, re-solve — incremental anchor addition, ≤8 rounds.
        st_lo = st_hi = None
        if demand_lo is not None and demand_hi is not None:
            st_lo = [max((demand_lo.get(i, float("-inf"))
                          for i in p["idxs"]),
                         default=float("-inf")) for p in positions]
            st_hi = [min((demand_hi.get(i, float("inf"))
                          for i in p["idxs"]),
                         default=float("inf")) for p in positions]
            # THRESHOLD-ENVELOPE FILTER: a chain bound that conflicts
            # with the runway's own locked anchors is jointly
            # infeasible (HECA: a 112.6 ceiling 51 m from the 116.5
            # 23C threshold would force a 7.6 % cliff) — the residual
            # belongs to the pavement (the terminal should yield), not
            # to a runway kink.  Drop any station bound that does not
            # intersect [anchor ∓ 1.5 %·distance] from every initially
            # anchored station; intersect the survivors.
            cap_r = _role_grade(ROLE_RUNWAY)

            def _pair_slack(fa, fb):
                """Max |Δelev| the FAA caps allow between two stations:
                0.8 % (RUNWAY_END_GRADE) over the portion of the span in
                the first/last RUNWAY_END_FRACTION, 1.5 % over the rest.
                Using the uniform cap accepted end-region anchors the
                solve could not legalise (05L 1.56 % at d=2963)."""
                a, b = sorted((fa * L, fb * L))
                end = RUNWAY_END_FRACTION * L
                o_lo = max(0.0, min(b, end) - min(a, end))
                o_hi = max(0.0, max(b, L - end) - max(a, L - end))
                mid = max(0.0, (b - a) - o_lo - o_hi)
                return RUNWAY_END_GRADE * (o_lo + o_hi) + cap_r * mid

            anch0 = [k for k in range(len(positions)) if anchored[k]]
            for k in range(len(positions)):
                env_lo = float("-inf")
                env_hi = float("inf")
                for j in anch0:
                    sl = _pair_slack(fractions[k], fractions[j])
                    if elevs[j] - sl > env_lo:
                        env_lo = elevs[j] - sl
                    if elevs[j] + sl < env_hi:
                        env_hi = elevs[j] + sl
                if st_lo[k] > env_hi or st_hi[k] < env_lo:
                    st_lo[k] = float("-inf")     # conflict: leave free
                    st_hi[k] = float("inf")
                else:
                    st_lo[k] = max(st_lo[k], env_lo)
                    st_hi[k] = min(st_hi[k], env_hi)
            # PAIRWISE CONSISTENCY (2026-06-10): a rise FLOOR at one
            # station and a dip CEILING at another that cannot both hold
            # at the runway cap along the chain (HECA 05C: floor 118.1
            # from the 05R network two metres from a 113.2 ceiling from
            # the T4 chain) would otherwise each be anchored in turn —
            # forcing a wall no FAA fit can legalise (measured 255 %
            # adj-grade, the whole flex then reverts).  Resolve by demand
            # DEPTH vs the incoming profile: the DEEPER demand carries
            # the larger committed tension and survives; the shallower
            # side returns to the pavement (the relief re-run pushes it
            # outward to whatever can yield).  Iterate until stable —
            # dropping one bound can clear a chain of conflicts.
            dep_lo = [(st_lo[k] - elevs[k]
                       if st_lo[k] > float("-inf") else 0.0)
                      for k in range(len(positions))]
            dep_hi = [(elevs[k] - st_hi[k]
                       if st_hi[k] < float("inf") else 0.0)
                      for k in range(len(positions))]
            changed = True
            while changed:
                changed = False
                for ka in range(len(positions)):
                    if dep_lo[ka] <= 0.05:
                        continue
                    for kb in range(len(positions)):
                        if dep_hi[kb] <= 0.05:
                            continue
                        if (st_lo[ka] - st_hi[kb]
                                <= _pair_slack(fractions[ka],
                                               fractions[kb]) + 0.01):
                            continue
                        if dep_lo[ka] < dep_hi[kb]:
                            st_lo[ka] = float("-inf")
                            dep_lo[ka] = 0.0
                        else:
                            st_hi[kb] = float("inf")
                            dep_hi[kb] = 0.0
                        changed = True
        _dbg_rs = _os.environ.get("O4_FLEX_DEBUG") == "1" and st_lo is not None
        banned: set = set()
        for _round in range(12):
            faa_joint_solve(fractions, elevs, anchored, L,
                            end_grade_cap=RUNWAY_END_GRADE,
                            end_fraction=RUNWAY_END_FRACTION)
            if st_lo is None:
                break
            worst_k = -1
            worst_ex = 0.05
            for k in range(len(positions)):
                if anchored[k] or k in banned:
                    continue
                if st_lo[k] > st_hi[k]:
                    continue               # infeasible station: leave free
                ex = max(elevs[k] - st_hi[k], st_lo[k] - elevs[k])
                if ex > worst_ex:
                    worst_ex = ex
                    worst_k = k
            if worst_k < 0:
                break
            # ANCHOR-CONSISTENCY GUARD: the clamped value must be cap-
            # reachable from EVERY existing anchor (the envelope filter
            # only checked the INITIAL anchors — an anchor added in an
            # earlier round can make a later bound jointly infeasible).
            v = min(max(elevs[worst_k], st_lo[worst_k]), st_hi[worst_k])
            ok = True
            for j in range(len(positions)):
                if not anchored[j]:
                    continue
                if (abs(v - elevs[j])
                        > _pair_slack(fractions[worst_k],
                                      fractions[j]) + 0.02):
                    ok = False
                    break
            if not ok:
                banned.add(worst_k)
                if _dbg_rs:
                    print(f"[flex]   resmooth {ref_key} r{_round}: BAN "
                          f"st{worst_k} d={positions[worst_k]['d']:.0f} "
                          f"bound [{st_lo[worst_k]:.2f},"
                          f"{st_hi[worst_k]:.2f}] vs anchors")
                continue
            if _dbg_rs:
                print(f"[flex]   resmooth {ref_key} r{_round}: anchor "
                      f"st{worst_k} d={positions[worst_k]['d']:.0f} "
                      f"elev {elevs[worst_k]:.2f} -> bound "
                      f"[{st_lo[worst_k]:.2f},{st_hi[worst_k]:.2f}]")
            elevs[worst_k] = v
            anchored[worst_k] = True
        if _dbg_rs:
            wg, wk = 0.0, -1
            for k in range(1, len(positions)):
                dd = positions[k]["d"] - positions[k - 1]["d"]
                if dd > 1e-6:
                    g = abs(elevs[k] - elevs[k - 1]) / dd
                    if g > wg:
                        wg, wk = g, k
            if wk >= 0:
                print(f"[flex]   resmooth {ref_key} final: worst "
                      f"adj-grade {wg*100:.2f}% at d="
                      f"{positions[wk]['d']:.0f} "
                      f"({elevs[wk-1]:.2f}->{elevs[wk]:.2f})")
        for p, e in zip(positions, elevs):
            for i in p["idxs"]:
                if i not in anchored_nodes:
                    elev[i] = e
        # ORPHAN-NODE SYNC (user 2026-06-09, HECA #174 blast pad): ring
        # nodes that are NOT chain stations — mid-edge conformance
        # inserts, e.g. a junction corner planted on a blast-pad long
        # edge where no segment seam exists, and every vertex of a
        # non-quad runway piece — are never written by the station loop
        # above, so the flex band-solve could leave a SINGLE node 0.8 m
        # above its flat piece (5.76 % within, #174 node at 61.5 on a
        # 60.7 blast pad).  Set each to the smoothed profile
        # interpolated at its axial position.
        station_idxs: set = set()
        for p in positions:
            station_idxs.update(p["idxs"])
        pts_ax = [pt for cs in rects for pt in cs]
        bestd = -1.0
        A = B = None
        for ii in range(len(pts_ax)):
            for jj in range(ii + 1, len(pts_ax)):
                dd = ((pts_ax[jj][0] - pts_ax[ii][0]) ** 2
                      + (pts_ax[jj][1] - pts_ax[ii][1]) ** 2)
                if dd > bestd:
                    bestd, A, B = dd, pts_ax[ii], pts_ax[jj]
        if A is None or bestd <= 0:
            continue
        axL = math.sqrt(bestd)
        ux, uy = (B[0] - A[0]) / axL, (B[1] - A[1]) / axL
        ds = [p["d"] for p in positions]
        cps_r = layout.canonical_points
        for s in layout.shapes:
            if (s.role != ROLE_RUNWAY or (s.ref or "") != ref_key
                    or s.polygon is None or s.polygon.is_empty):
                continue
            for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
                idx = bucket_to_idx.get(cps_r.get_or_add(float(x), float(y)))
                if idx is None or idx in station_idxs:
                    continue
                if idx in anchored_nodes:
                    continue
                d_n = (x - A[0]) * ux + (y - A[1]) * uy
                if d_n <= ds[0]:
                    elev[idx] = elevs[0]
                    continue
                if d_n >= ds[-1]:
                    elev[idx] = elevs[-1]
                    continue
                for k in range(1, len(ds)):
                    if d_n <= ds[k]:
                        span = ds[k] - ds[k - 1]
                        t = ((d_n - ds[k - 1]) / span) if span > 1e-9 else 0.0
                        elev[idx] = (elevs[k - 1]
                                     + t * (elevs[k] - elevs[k - 1]))
                        break


# A junction vertex within this distance of a sloping rect's / runway's
# long edge is treated as HUGGING/SHADOWING it (the vertex-push pass keeps
# a designed 1.0 m standoff; accumulated drift puts shadow-edge endpoints
# at up to ~1.8 m — SPJC's stepping endpoint sat 1.74 m off) and takes the
# edge-plane altitude, so a straight shadowing edge lerps along the plane.
_EDGE_PLANE_SNAP_DIST_M = 2.0


def _snap_junction_verts_to_rect_edge_plane(layout, elev, bucket_to_idx,
                                            shape_constraints, base_hard,
                                            owners) -> int:
    """Post-solve, altitude-only: every junction vertex within
    ``_EDGE_PLANE_SNAP_DIST_M`` of a sloping rect's or RUNWAY's long ring
    segment takes the segment's interpolated altitude (the vertex was
    deliberately pushed just off that edge — the surfaces must meet across
    the designed gap); the junction's interior is then locally
    re-projected with the snapped + shared + hard nodes held.  Returns the
    number of snapped vertices."""
    rect_edges: list = []
    for s in layout.shapes:
        if s.role not in SLOPING_RECT_ROLES and s.role != ROLE_RUNWAY:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        idxs = [bucket_to_idx.get(
            layout.canonical_points.get_or_add(float(x), float(y)))
            for x, y in ring]
        m = len(ring)
        for a in range(m):
            b = (a + 1) % m
            if idxs[a] is None or idxs[b] is None:
                continue
            ax, ay = ring[a]
            bx, by = ring[b]
            seg = math.hypot(bx - ax, by - ay)
            if seg >= 3.0:
                rect_edges.append((ax, ay, bx, by, seg,
                                   idxs[a], idxs[b]))
    if not rect_edges:
        return 0
    CELL = 50.0
    grid: dict = {}
    for k, (ax, ay, bx, by, seg, ia, ib) in enumerate(rect_edges):
        for gx in range(int(min(ax, bx) // CELL),
                        int(max(ax, bx) // CELL) + 1):
            for gy in range(int(min(ay, by) // CELL),
                            int(max(ay, by) // CELL) + 1):
                grid.setdefault((gx, gy), []).append(k)
    n_snapped = 0
    sc_by_nodes = {frozenset(sc["nodes"]): sc for sc in shape_constraints
                   if sc["role"] == ROLE_JUNCTION}
    for sh in layout.shapes:
        if (sh.role != ROLE_JUNCTION or sh.polygon is None
                or sh.polygon.is_empty):
            continue
        ring = _open_ring(list(sh.polygon.exterior.coords))
        idxs = [bucket_to_idx.get(
            layout.canonical_points.get_or_add(float(x), float(y)))
            for x, y in ring]
        sc = sc_by_nodes.get(frozenset(i for i in idxs if i is not None))
        if sc is None:
            continue
        moved: set = set()
        for (x, y), i in zip(ring, idxs):
            if i is None or base_hard[i]:
                continue
            gx0, gy0 = int(x // CELL), int(y // CELL)
            cand: set = set()
            for dgx in (-1, 0, 1):
                for dgy in (-1, 0, 1):
                    cand.update(grid.get((gx0 + dgx, gy0 + dgy), ()))
            best = None
            bd = _EDGE_PLANE_SNAP_DIST_M
            for k in cand:
                ax, ay, bx, by, seg, ia, ib = rect_edges[k]
                t = (((x - ax) * (bx - ax) + (y - ay) * (by - ay))
                     / (seg * seg))
                if t < 0.01 or t > 0.99:
                    continue                  # corner region: shared node
                px, py = ax + t * (bx - ax), ay + t * (by - ay)
                d = math.hypot(x - px, y - py)
                if d < bd:
                    bd = d
                    best = elev[ia] + t * (elev[ib] - elev[ia])
            if best is not None and abs(elev[i] - best) > 0.05:
                elev[i] = best
                moved.add(i)
                n_snapped += 1
        if moved and sc["edges"]:
            held_j = {i for i in sc["nodes"]
                      if base_hard[i] or owners.get(i, 0) > 1} | moved
            if len(held_j) < len(sc["nodes"]):
                _project_shape(elev, sc["nodes"], held_j, sc["edges"],
                               False)
    if _os.environ.get("O4_STEP_DEBUG") == "1":
        print(f"[step] edge-plane snap: {n_snapped} junction vertex(es) "
              f"took rect/runway edge-plane altitudes")
    return n_snapped


# Route-distance measurement uncertainty, as a fraction of the route length.
# The taxi-route graph under-counts real taxi paths: endpoint stubs are
# straight chords (the centerline rows stop short of runway edges) and row
# joins are uncurved corners (no fillets).  Measured at HECA's A4↔T4 corridor
# (s73): graph 3,217 m vs the ≥3,353 m reality requires — ~4 % short.  A route
# demand below ``frac · cap · route_d`` is within measurement noise of a
# feasible corridor; the demand synthesis drops it instead of flexing a runway.
# Value lives in config (single source of truth — the route-field bands and
# the validator use the SAME margin); re-exported under the historical name.
_ROUTE_NOISE_FRAC = ROUTE_NOISE_FRAC


def _flex_route_bands(layout, elev, bucket_to_idx, free_nodes,
                      thresh_nodes, terminal_nodes, cap,
                      flex_ref=None):
    """``{node: (lo, hi)}`` for each freed runway node, bounded by
    CENTERLINE-ROUTED anchors per the s65/s66 doctrine (user-
    authoritative): runway reachability uses the taxiway ROUTE, never
    a cross-apron chord (the geodesic edge graph under-counts and
    manufactures false demand — 05C read hi=102.1 via a ~2,030 m
    chord chain; even the 'real' 21-hop audit chain rides an 839 m
    cross-apron geodesic hop, the same artifact class).

    PER-RUNWAY mode (``flex_ref``): bound ONLY that runway's nodes;
    anchors = held terminals + ALL runway thresholds + the OTHER
    runways' pavement-contact nodes at their CURRENT elevations (the
    inter-runway demand carrier — e.g. Exit-3's pin chains through
    junction/L to the 05C network); the flexing runway's OWN
    centerline segments are NOT added to the graph, so its threshold
    cannot ride its own interior as a fictitious 1.5 % rise-corridor
    (the false 102.09 ceiling)."""
    from auto_patch.taxi_routing import (
        augment_with_runway_centerlines, shared_taxi_route_graph)
    cps = layout.canonical_points
    G = shared_taxi_route_graph(layout)
    if not getattr(G, "coord", None):
        return {}
    # AUGMENT a COPY of the shared graph with RUNWAY CENTERLINES
    # (2026-06-09, centralized in taxi_routing): the apt.dat taxi-route
    # rows stop at/near the runway edge, so threshold anchors were
    # unreachable and the legitimate other-runway demand (e.g. HECA 05L
    # 60.7 + 1.5 %·~3.2 km ≈ 108.5 at the T4 join) never formed.
    # ``skip_ref`` excludes the FLEXING runway's own pieces so its
    # threshold cannot ride its own interior as a fictitious 1.5 %
    # rise-corridor (the false 102.09 ceiling).  Local to the flex — the
    # terminal seed keeps the unaugmented graph.
    G = G.copy()
    augment_with_runway_centerlines(G, layout, skip_ref=flex_ref)
    free_set = set(free_nodes)
    # Pavement-contact nodes per runway (a runway ring node also used
    # by a non-runway pavement shape): contacts of OTHER runways are
    # demand anchors at their current elevations in per-runway mode.
    nonrwy_nodes: set = set()
    for s in layout.shapes:
        if (s.role == ROLE_RUNWAY or s.role not in PAVEMENT_ROLES
                or s.polygon is None or s.polygon.is_empty):
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            idx = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if idx is not None:
                nonrwy_nodes.add(idx)
    pos: dict = {}
    anchor_pts: list = []                  # (key, gap, elev)
    seen_anchor: set = set()
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
            continue
        is_term = s.role == ROLE_TERMINAL
        is_rwy = s.role == ROLE_RUNWAY
        if not (is_term or is_rwy):
            continue
        is_other_rwy = (is_rwy and flex_ref is not None
                        and (s.ref or "") != flex_ref)
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            idx = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if idx is None:
                continue
            if is_rwy and idx in free_set and idx not in pos:
                pos[idx] = (x, y)
            is_anchor = (
                (is_term and idx in terminal_nodes)
                or (is_rwy and idx in thresh_nodes)
                or (is_other_rwy and idx in nonrwy_nodes))
            if is_anchor and idx not in seen_anchor:
                seen_anchor.add(idx)
                akey, agap = G.nearest_key(x, y)
                if akey is not None and agap < 60.0:
                    anchor_pts.append((akey, agap, elev[idx]))
    if not pos or not anchor_pts:
        return {}
    bands: dict = {}
    _dbg_band = _os.environ.get("O4_FLEX_BAND_DEBUG") == "1"
    for i, (x, y) in pos.items():
        dm, sg = G.distances_from((x, y))
        if dm is None:
            continue
        lo_i, hi_i = float("-inf"), float("inf")
        d_lo = d_hi = 0.0
        lo_a = hi_a = None
        for akey, agap, ae in anchor_pts:
            d = dm.get(akey)
            if d is None:
                continue
            dtot = d + sg + agap
            slack = cap * dtot
            if ae - slack > lo_i:
                lo_i = ae - slack
                d_lo = dtot
                lo_a = (akey, d, ae)
            if ae + slack < hi_i:
                hi_i = ae + slack
                d_hi = dtot
                hi_a = (akey, d, ae)
        if lo_i > float("-inf") or hi_i < float("inf"):
            # (lo, hi, binding-anchor route distance for each bound) —
            # the distances feed the route-noise deadband in the demand
            # synthesis (measurement uncertainty scales with route length).
            bands[i] = (lo_i, hi_i, d_lo, d_hi)
            if _dbg_band:
                def _fmt(a):
                    if a is None:
                        return "-"
                    akey, d, ae = a
                    pt = G.coord.get(akey)
                    return (f"e={ae:.1f}@d={d:.0f}m"
                            f"({pt[0]:.0f},{pt[1]:.0f})")
                print(f"[band] n{i} ({x:.0f},{y:.0f}) "
                      f"lo={lo_i:.2f}<-{_fmt(lo_a)} "
                      f"hi={hi_i:.2f}<-{_fmt(hi_a)}")
    return bands


def _relax_runway_and_resolve(n, elev, layout, bucket_to_idx, base_hard,
                              shape_constraints, tol_m,
                              relief_eg=None, relief_el=None) -> int:
    """Third pass (user 2026-05-28): if a within-shape grade violation remains
    after the reverse pass, free part of the runway and re-run the difference-
    constraint band solve over the combined pavement + runway grade graph so an
    impossible apron<->runway connection spreads over the runway's length
    instead of concentrating on a short connector.

    Two escalation levels, tried in order, taking the FIRST that strictly
    reduces the worst within-shape violation:
      1. Free the runway INTERIOR only — CIFP thresholds stay hard.  Fixes a
         DEM-dipped runway interior dragging an adjacent junction (CYXY 14R/32L).
      2. (last resort, only when a tile-boundary SEAM exists) ALSO release the
         CIFP THRESHOLD endpoints.  Per the priority ``seam > runway-CIFP``: when
         the seam terrain makes the runway<->seam connection physically
         infeasible (band lo > hi, e.g. SPLP runway 73 m vs a seam node 62 m too
         close to grade), the runway yields — the whole runway shifts/tilts the
         minimum (seeded at CIFP, so feasible ends stay put) to restore grade.
    Seam-pinned runway nodes NEVER move.  Commits only if a level improves;
    otherwise reverts (no regression).  Returns sweeps used."""
    if not _RUNWAY_FLEX:
        return 0
    pav_edges = [e for sc in shape_constraints for e in sc["edges"]]
    comply = max(tol_m, _SPREAD_COMPLY_TOL_M)
    w0, c0, t0 = _within_excess_stats(elev, pav_edges, comply)
    if c0 == 0:
        return 0                                  # already grade-compliant

    rwy_constraints = _build_runway_constraints(layout, bucket_to_idx)
    if not rwy_constraints:
        return 0
    thresh = _runway_threshold_nodes(layout, bucket_to_idx)
    seam_pinned = _seam_pinned_runway_nodes(layout, bucket_to_idx)
    crossing = _runway_crossing_nodes(layout, bucket_to_idx)
    rwy_nodes: set = set()
    for sc in rwy_constraints:
        rwy_nodes.update(sc["nodes"])
    interior = rwy_nodes - thresh - seam_pinned
    if not interior:
        return 0                                  # nothing to free

    all_edges = pav_edges + [e for sc in rwy_constraints for e in sc["edges"]]
    coupling = _build_level_coupling(list(shape_constraints) + rwy_constraints)
    terminal_nodes: set = set()
    for sc in shape_constraints:
        if sc["flat"]:
            terminal_nodes.update(sc["nodes"])

    # Escalating free-sets.  Thresholds are released ONLY as a last resort and
    # ONLY when a seam exists to yield to (seam > CIFP).
    levels = [interior]
    if seam_pinned or getattr(layout, "_seam_anchor_keys", None):
        levels.append(interior | (thresh - seam_pinned))

    # The RUNWAY must never be pulled OUT of grade compliance to chase an apron
    # / taxiway violation (user 2026-06-02): a runway has strict invariants
    # (1.5 % mid, 0.8 % ends) and is higher priority than the connection.  Only
    # accept a flex level if the moved runway chain itself stays compliant —
    # otherwise the connection must yield (or, as a last resort, the thresholds
    # release, which is the level-2 free-set), not the runway go out of grade.
    rwy_edges = [e for sc in rwy_constraints for e in sc["edges"]]
    rwy_node_set: set = set()
    for sc in rwy_constraints:
        rwy_node_set.update(sc["nodes"])
    snapshot0 = list(elev)
    # Pre-flex runway compliance (grade + FAA vertical curve) — the bar the
    # flexed-then-smoothed runway must still clear.  The band solve spreads an
    # apron↔runway violation by tilting/dropping the runway interior, which is
    # grade-feasible per-edge but PIECEWISE-LINEAR — it leaves kinks at the flex
    # boundaries (HECA 05C/23C: 1→9 kinks, |Δg| 0.0095→0.0285/m).  Per the user's
    # 2026-06-05 spec the runway pulls ONLY within its grade+curvature slack: so
    # after the flex we RE-SMOOTH the runway to a genuinely FAA-compliant profile
    # (full centerline) and re-grade the pavement against it.  A benign flatten
    # (CYXY stub A) survives smoothing and still helps; a destructive interior
    # drop (HECA, fixed thresholds) gets clamped back by the FAA envelope so it
    # no longer helps → falls through to step-3 threshold relief.
    rwy_g0, rwy_curv0 = _runway_profile_compliance(
        layout, snapshot0, bucket_to_idx)
    _dbg = _os.environ.get("O4_FLEX_DEBUG") == "1"
    # FLEX DEMAND SYNTHESIS gate (s68-close design; DEFAULT ON since s73 —
    # user 2026-06-10 evaluation; O4_FLEX_MIN_CLAMP=0 restores the legacy
    # combined-band flex).  Only when the airport has NO runway-runway
    # crossings (interim guard, 2026-06-09): with a crossing, anchoring the
    # flexed second runway COMMITS the dormant crossing-anchor injection bug
    # (s65 OPEN — the agreed E_x never lands in the second runway's profile;
    # CYXY 14L/32R floats to 695.9 vs 02/20's 693.7).  Lift this guard when
    # the injection fix lands in runway_segments.
    _clamp_on = (not crossing
                 and _os.environ.get("O4_FLEX_MIN_CLAMP", "1") == "1")
    if _dbg:
        print(f"[flex] seam_pinned={len(seam_pinned)} "
              f"seam_keys={len(getattr(layout, '_seam_anchor_keys', None) or [])} "
              f"levels={len(levels)} c0={c0} w0={w0:.3f} "
              f"rwy_g0={rwy_g0*100:.2f}% rwy_curv0={rwy_curv0}")
    sweeps_total = 0
    committed_soft: set | None = None
    for _li, soft in enumerate(levels):
        if not soft:
            continue
        is_hard2 = list(base_hard)
        for i in soft:
            is_hard2[i] = False
        thr_freed = bool(soft & thresh)
        elev[:] = snapshot0                       # each level restarts clean
        # (a) Coupled flex: free the runway interior (+ thresholds for the seam
        #     level) and the pavement, solve the combined grade bands.  When the
        #     thresholds are freed this lets the runway TILT to the hard seam
        #     anchor (seam > CIFP) — the tilt must NOT be undone.
        if _clamp_on and not thr_freed:
            # DEMAND-SYNTHESIS path: the (a) settle is SKIPPED.  Demands are
            # measured at the held-runway saturation state — which is exactly
            # ``snapshot0`` (steps 1-2 ran with every runway node hard), and
            # the level restart above just reset ``elev`` to it.  Running (a)
            # here would only manufacture the chord-graph over-dip the
            # synthesis exists to replace (terminal7→05C reads ~2,030 m on
            # the constraint graph where the real route is ~3,100 m → false
            # 102.1 demand).
            pass
        else:
            lo, hi = _grade_bands(n, elev, is_hard2, all_edges)
            sweeps, _viol = _project_within_bands(
                elev, all_edges, is_hard2, lo, hi, coupling,
                held_extra=terminal_nodes, max_sweeps=1000, tol=comply)
            sweeps_total += sweeps
            if _os.environ.get("O4_FLEX_DEBUG") == "1":
                wa, ca, _ta = _within_excess_stats(elev, pav_edges, comply)
                print(f"[flex]  level{_li} after combined-band (a): "
                      f"pav within c={ca} worst={wa:.3f}")
        if not thr_freed:
            # INTERIOR flex (thresholds locked at CIFP).  The combined band
            # solve in (a) settles the runway toward the connecting demand,
            # but its 50/50 violation split is NOT minimal: it drags the
            # runway BELOW the true demand toward pavement that could itself
            # have risen (user 2026-06-09: 05C/23C over-dipped to ~102 where
            # the route-feasible minimum is ~108).  MINIMAL-FLEX CLAMP: pull
            # every freed runway node back toward its PRE-FLEX profile
            # within the feasibility band measured against the SETTLED
            # pavement (held); re-grade the pavement toward the lifted
            # runway; clamp once more to capture the second-order lift.
            # snapshot0 is cap-feasible (the FAA pre-flex profile).  The
            # bound must come from CENTERLINE-ROUTED anchors (terminals +
            # thresholds): geodesic bands are doubly wrong — against the
            # settled pavement they're circular (pavement settled WITH the
            # over-dipped runway at cap saturation), and against the true
            # anchors the edge-graph chord-cuts junctions / shortcuts
            # aprons and manufactures false demand (05C hi=102.1 via a
            # 2,030 m chain where the real route allows ~108+).
            # FLEX DEMAND SYNTHESIS (s68-close design, gated
            # O4_FLEX_MIN_CLAMP=1): per-runway demand at each PAVEMENT
            # CONTACT = min(contact-edge EXCESS at the held-runway
            # saturation state, ROUTE-BAND justified depth).
            #   - The excess term is the user model verbatim (pavement
            #     grades to max FIRST, then the runway flexes the minimum
            #     so every junction meets it), expressed as a BAND: the
            #     directional relief pushes violations OUTWARD, so at
            #     ``snapshot0`` the contact EDGE itself reads compliant
            #     and the saturation sits deeper in the network — the
            #     demand is the gap between the runway's profile and the
            #     pavement network's own feasible band at the contact
            #     (``_grade_bands`` over the pavement edges with the
            #     flexing runway UNPINNED).  This term sees PAVEMENT-
            #     INTERNAL pins (Exit-3's 0.9 m via the #207↔L chain to a
            #     held 05C contact) that route-to-anchor bounds cannot.
            #   - The route-band term caps the MAGNITUDE per the s65/s66
            #     doctrine: when a real taxi route to a hard anchor itself
            #     justifies a flex at the contact, take the SHALLOWER of
            #     the two depths — saturated excess beyond the band is the
            #     geodesic-chord measurement-artifact class (T4 dips to
            #     ~108.5, the 05L-route level, never the chord ~104).
            #     When the band has no opinion (Exit-3), the local contact
            #     excess stands alone — a single edge cannot chord-cut.
            # Bounds are applied ONLY through the bounded re-smooth below
            # (threshold-envelope filter + iterative anchor addition); raw
            # clamping lifted 05L to a 7.3 % wall 30 m from its locked
            # threshold (within 41→176).
            if _clamp_on:
                _dbg2 = _os.environ.get("O4_FLEX_DEBUG") == "1"
                soft_set = set(soft)
                ref_nodes: dict = {}
                for s in layout.shapes:
                    if (s.role != ROLE_RUNWAY or s.polygon is None
                            or s.polygon.is_empty):
                        continue
                    rr = s.ref or ""
                    for (x, y) in _open_ring(
                            list(s.polygon.exterior.coords)):
                        idx = bucket_to_idx.get(
                            layout.canonical_points.get_or_add(
                                float(x), float(y)))
                        if idx is not None and idx in soft_set:
                            ref_nodes.setdefault(rr, set()).add(idx)
                pav_node_set: set = set()
                for sc in shape_constraints:
                    pav_node_set.update(sc["nodes"])
                # CHAIN graph for term 1 = pavement edges WITHOUT the
                # apron shapes' visibility chords: a runway-demand chain
                # that rides ACROSS an apron interior is the s66
                # measurement-artifact class (05L read +10.5 m floors
                # from cross-apron chains where the real route needs
                # none).  Taxiway-rect/junction/stub chains stay — they
                # are the carrier of pavement-internal pins the route
                # graph cannot see (Exit-3 via #207↔L).
                pav_edges_t1 = [e for sc in shape_constraints
                                if sc["role"] != ROLE_APRON
                                for e in sc["edges"]]

                def _ref_demands(rr):
                    """(all_contacts, rise_d, dip_d) for runway ``rr`` at
                    the CURRENT ``elev`` (committed refs included).

                    Saturation demand (term 1, the CHAIN measure): the
                    apron-free pavement network's feasible band at the
                    contact, anchored at {held terminal seeds +
                    thresholds/seam + the OTHER runways' nodes at
                    current values}, the flexing runway's own soft nodes
                    UNPINNED.  A band ceiling below the runway's profile
                    means every rect/junction path from the contact to
                    some anchor is saturated at cap — "pavement graded
                    to max first"; the residual gap is the runway's to
                    flex.
                    """
                    own = ref_nodes[rr]
                    all_contacts = {i for i in own if i in pav_node_set}
                    if not all_contacts:
                        return all_contacts, {}, {}
                    is_hard_p = list(base_hard)
                    for i in terminal_nodes:
                        is_hard_p[i] = True
                    for i in rwy_node_set - own:
                        is_hard_p[i] = True
                    lo_p, hi_p = _grade_bands(n, elev, is_hard_p,
                                              pav_edges_t1)
                    rise_d: dict = {}
                    dip_d: dict = {}
                    for i in all_contacts:
                        s0 = snapshot0[i]
                        if s0 - hi_p[i] > 0.05:       # 5 cm noise floor
                            dip_d[i] = s0 - hi_p[i]
                        elif lo_p[i] - s0 > 0.05:
                            rise_d[i] = lo_p[i] - s0
                    return all_contacts, rise_d, dip_d

                # SEQUENTIAL per-ref commit, DEEPEST demand first: each
                # committed runway anchors the next ref's measurement
                # (05C must dip to ~108.5 BEFORE 05L's bands are read, or
                # 05L sees the unflexed 110-116 contacts as anchors and
                # manufactures an inflated rise).  Order by the max
                # demand depth measured against the pre-flex state.
                depth0: dict = {}
                for rr in sorted(ref_nodes):
                    _c, r_d, d_d = _ref_demands(rr)
                    depth0[rr] = max([*r_d.values(), *d_d.values()],
                                     default=0.0)
                for rr in sorted(ref_nodes,
                                 key=lambda r: -depth0[r]):
                    all_contacts, rise_d, dip_d = _ref_demands(rr)
                    lo_b: dict = {}
                    hi_b: dict = {}
                    contacts = all_contacts
                    if contacts:
                        # Route bands at EVERY contact (term 2): anchors
                        # = held terminals + ALL thresholds + OTHER
                        # runways' pavement contacts at current values,
                        # own centerline excluded (no fictitious 1.5 %
                        # rise-corridor).  The demand depth = min over
                        # the opinions that EXIST: the chain (term 1)
                        # carries pavement-internal pins the route
                        # cannot see (Exit-3); the route carries apron-
                        # borne demand the apron-free chain cannot see
                        # (T4) and CAPS the chain where both measure
                        # (route bands are authoritative, user
                        # 2026-06-09).  A one-sided "no demand" from
                        # either measure is absence of evidence, not a
                        # veto — bands are upper/lower bounds, not
                        # assertions the other path's demand is false.
                        bands = _flex_route_bands(
                            layout, elev, bucket_to_idx, contacts,
                            thresh, terminal_nodes, TAXI_MAX_GRADE,
                            flex_ref=rr)
                        n_dip = n_rise = 0
                        for i in contacts:
                            in_band = (i in bands
                                       and bands[i][0] <= bands[i][1])
                            blo, bhi, bdlo, bdhi = (
                                bands[i] if in_band
                                else (float("-inf"), float("inf"),
                                      0.0, 0.0))
                            s0 = snapshot0[i]
                            # ROUTE-NOISE DEADBAND (s73, measured at the
                            # A4↔T4 corridor): the route graph reads
                            # 3,217 m where reality (A4≈60, T4≈110 at
                            # ≤1.5 %) needs ≥3,353 m — straight endpoint
                            # stubs (125 m here) + uncurved row joins
                            # under-count by ~4 %.  A route demand
                            # smaller than that distance-proportional
                            # uncertainty is indistinguishable from a
                            # feasible corridor and must NOT flex a
                            # runway (A4's false +1.6 m rise).  Demands
                            # that clear the deadband keep their FULL
                            # point estimate (T4's 4.3 m dip → 110.9;
                            # shrinking by the noise would under-flex).
                            noise_r = max(0.05, _ROUTE_NOISE_FRAC
                                          * TAXI_MAX_GRADE * bdlo)
                            noise_d = max(0.05, _ROUTE_NOISE_FRAC
                                          * TAXI_MAX_GRADE * bdhi)
                            route_r = ((blo - s0) if in_band else None)
                            if route_r is not None and route_r <= noise_r:
                                route_r = None
                            route_d = ((s0 - bhi) if in_band else None)
                            if route_d is not None and route_d <= noise_d:
                                route_d = None
                            cands_r = [d for d in (rise_d.get(i), route_r)
                                       if d is not None and d > 0.05]
                            if cands_r:
                                lo_b[i] = min(s0 + min(cands_r), bhi)
                                n_rise += 1
                            cands_d = [d for d in (dip_d.get(i), route_d)
                                       if d is not None and d > 0.05]
                            if cands_d:
                                hi_b[i] = max(s0 - min(cands_d), blo)
                                n_dip += 1
                            if _dbg2:
                                xy = next(
                                    ((x, y) for s2 in layout.shapes
                                     if s2.role == ROLE_RUNWAY
                                     and (s2.ref or "") == rr
                                     and s2.polygon is not None
                                     for (x, y) in _open_ring(list(
                                         s2.polygon.exterior.coords))
                                     if bucket_to_idx.get(
                                         layout.canonical_points
                                         .get_or_add(float(x), float(y)))
                                     == i), None)
                                print(
                                    f"[flex]   contact {rr} n{i} "
                                    f"xy={xy} s0={snapshot0[i]:.2f} "
                                    f"t1=(r{rise_d.get(i, 0):.2f}/"
                                    f"d{dip_d.get(i, 0):.2f}) "
                                    f"t2=[{blo:.2f},{bhi:.2f}] -> "
                                    f"lo={lo_b.get(i)} hi={hi_b.get(i)}")
                        if _dbg2:
                            print(f"[flex]  demand-synth {rr}: "
                                  f"{len(contacts)} demand contacts "
                                  f"({n_dip} dip / {n_rise} rise), "
                                  f"{len(bands)} route-banded")
                    # Bounded re-smooth of THIS ref only (envelope
                    # filter + pairwise consistency + iterative anchor
                    # loop); its committed stations then anchor the
                    # next ref's demand measurement.
                    _resmooth_runways_in_elev(
                        layout, elev, bucket_to_idx,
                        thresh | seam_pinned | crossing,
                        demand_lo=lo_b, demand_hi=hi_b,
                        only_refs={rr})
            else:
                # (no in-block re-grade: the re-smooth produces the final
                # profile; the hard3 re-grade after it brings the
                # pavement to that profile in one pass.)
                # Re-smooth to fold in the FAA vertical-curve / end-grade
                # the band solve doesn't model, anchored at the locked
                # thresholds.
                _resmooth_runways_in_elev(
                    layout, elev, bucket_to_idx,
                    thresh | seam_pinned | crossing)
            # Re-grade the pavement against the smoothed, HELD runway: hold the
            # WHOLE runway so the band solve cannot re-pull its interior down
            # toward low aprons (which would undo the flat profile).
            hard3 = list(base_hard)
            for i in rwy_node_set:
                hard3[i] = True
            if _clamp_on and relief_eg:
                # FULL relief re-run against the committed profile (s68
                # measured: ONE band re-grade cannot redistribute 3-6 m
                # profile moves through the network — within 41→167; the
                # outward shape-cascade can).  Pavement is still at the
                # saturation state (only runway stations moved above), so
                # this is exactly STEP 2 re-run with the flexed runway as
                # the hard anchor set; terminals stay held at their seeds.
                sweeps3 = _directional_relief(
                    n, elev, hard3, relief_eg, relief_el,
                    shape_constraints, _RELIEF_MAX_ITERS, tol_m)
            else:
                lo3, hi3 = _grade_bands(n, elev, hard3, all_edges)
                sweeps3, _v3 = _project_within_bands(
                    elev, all_edges, hard3, lo3, hi3, coupling,
                    held_extra=terminal_nodes, max_sweeps=1000, tol=comply)
            sweeps_total += sweeps3
            w1, c1, t1 = _within_excess_stats(elev, pav_edges, comply)
            rwy_g1, rwy_curv1 = _runway_profile_compliance(
                layout, elev, bucket_to_idx)
            # Commit iff the runway stays GRADE compliant.  Curvature is
            # deliberately NOT a gate here: the emit applies a SPLINE profile to
            # the long runway rects (``layout._slope_profile_for`` → "spline" for
            # rects > 300 m), which smooths the transition into the lowered
            # route-band-dip segment in the baked surface.  So the marginal kink
            # the DISCRETE chain measure reports (``rwy_curv1``, computed only for
            # the debug line) is a false positive — it does not exist after the
            # spline — and must not block a grade-compliant, inter-runway-feasible
            # MINIMUM dip (HECA 05C→~108.5 for the 05L/23R route was being
            # rejected by ``rwy_curv1<=rwy_curv0`` against a flat seed, where any
            # dip reads as +1 kink).  Pavement within-shape is also NOT required
            # to improve: holding the runway at its inter-runway-feasible profile
            # exposes apron-fill gaps honestly rather than masking them.
            improved = rwy_g1 <= max(rwy_g0, _role_grade(ROLE_RUNWAY)) + 1e-4
        else:
            # SEAM level — the hard tile-seam anchor (seam > CIFP) makes the
            # runway↔seam connection infeasible at the CIFP thresholds, so the
            # band solve TILTS the runway (thresholds yield) to a profile that
            # passes through the seam.  That seam-driven tilt is the correct
            # shape — re-smoothing it threshold-to-threshold would ignore the
            # mid-runway seam constraint and reintroduce grade.  Accept it iff
            # the tilted runway is itself grade compliant (≤ cap, ABSOLUTE — the
            # yield's whole purpose) and does not worsen curvature beyond the
            # pre-flex baseline.  (Fully eliminating the tilt's residual
            # vertical-curve kink needs the seam's runway-crossing node anchored
            # in the FAA smooth — tracked as the seam-curvature follow-up.)
            w1, c1, t1 = _within_excess_stats(elev, pav_edges, comply)
            rwy_g1, rwy_curv1 = _runway_profile_compliance(
                layout, elev, bucket_to_idx)
            improved = (
                w1 <= w0 + comply
                and (c1 < c0 or t1 < t0 - comply)
                and rwy_curv1 <= rwy_curv0 + _SEAM_CURV_KINK_ALLOWANCE
                and rwy_g1 <= _role_grade(ROLE_RUNWAY) + 1e-4)
        if _dbg:
            print(f"[flex]  level{_li} free={len(soft)} thr_freed="
                  f"{bool(soft & thresh)} -> c1={c1} w1={w1:.3f} "
                  f"rwy_g1={rwy_g1*100:.2f}% rwy_curv1={rwy_curv1} "
                  f"improved={improved}")
        if improved:
            committed_soft = soft                 # this level helped — keep it
            break
    if committed_soft is None:
        elev[:] = snapshot0                       # no level helped -> revert
        return sweeps_total

    # Commit: write the moved runway profile back.  ``_writeback`` skips clean
    # 4-corner runway rects (keeps CIFP altitude_high/low) and never touches
    # ROLE_RUNWAY_CROSSING at all, so a runway/crossing shape with a moved
    # node must be converted to per-vertex ``node_altitudes`` here or its shared
    # corner with the (written-back) junction would mismatch.  Only convert
    # shapes that actually moved; unmoved threshold-end rects keep CIFP hi/lo.
    for s in layout.shapes:
        if s.role not in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING):
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        coords = list(s.polygon.exterior.coords)
        ring_closed = bool(coords) and coords[0] == coords[-1]
        coords_open = coords[:-1] if ring_closed else coords
        moved = False
        alts = []
        for (x, y) in coords_open:
            idx = bucket_to_idx.get(
                layout.canonical_points.get_or_add(float(x), float(y)))
            if idx is None:
                alts = None
                break
            alts.append(round(float(elev[idx]), 1))
            if idx in committed_soft and abs(elev[idx] - snapshot0[idx]) > comply:
                moved = True
        if not moved or alts is None:
            continue
        if ring_closed:
            alts.append(alts[0])
        s.node_altitudes = alts
        s.altitude_high = None
        s.altitude_low = None
        s.altitude = None
    return sweeps_total


_CORRIDOR_ROLES = (ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
                   ROLE_STUB, ROLE_CROSS_CONNECTOR)
# Continuation gate: the exit direction, the across-junction gap vector and
# the next rect's entry direction must all agree within 45°.
_CORRIDOR_COS_MIN = 0.71


def _interp_profile(ds, es, dq):
    """Piecewise-linear profile lookup (corridor station chain)."""
    if dq <= ds[0]:
        return es[0]
    for k in range(1, len(ds)):
        if dq <= ds[k]:
            span = ds[k] - ds[k - 1]
            if span < 1e-9:
                return es[k]
            t = (dq - ds[k - 1]) / span
            return es[k - 1] + t * (es[k] - es[k - 1])
    return es[-1]


def _network_field_stations(layout, elev, bucket_to_idx, chain_data,
                            rwy_nodes, rwy_ref_of, nodes, exit_overrides,
                            dem_ctx, base_hard=None):
    """NETWORK PROFILE MODEL (#4): solve ONE elevation field over the full
    centerline graph (``auto_patch.network_profile``) and assign every
    corridor station the FIELD value at its position — shared physical
    points (crossing inserts, co-located mouths, junction networks) agree
    by construction, so the tie/consensus/freeze layer has nothing to
    stitch.  Returns the DIP-only runway-flex demand dicts
    ``(dem_lo, dem_hi, dem_refs)`` measured directly on the field (M3:
    the demand = the profile value the network wants at the contact).

    The solved field is stored on ``layout._network_profile_field`` for
    the apron geodesic seeds (M6) and the validator (route_field — the
    same-field law, §7 validator simultaneity)."""
    from auto_patch import network_profile as _np
    cps = layout.canonical_points
    rings = []
    for s in layout.shapes:
        if (s.role != ROLE_RUNWAY or s.polygon is None
                or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        elevs = []
        for (x, y) in ring:
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            elevs.append(elev[i] if i is not None else None)
        rings.append((ring, elevs, s.ref or ""))
    seed_at = None
    if dem_ctx is not None and dem_ctx[0] is not None:
        dem9, tlat9, tlon9 = dem_ctx
        from auto_patch.elevation import _sample_dem

        def seed_at(x, y):
            try:
                lat, lon = layout.m_to_ll(x, y)
                e9 = _sample_dem(dem9, tlat9, tlon9, lat, lon)
            except _GEOM_EXC:
                return None
            return float(e9) if e9 is not None else None

    # current-surface fallback (anchor-less components smooth the graded
    # surface, never re-derive raw terrain) — nearest solver node ≤60 m
    fallback_at = None
    if nodes is not None and len(nodes):
        _fcell: dict = {}
        for i9 in range(len(nodes)):
            x9, y9 = nodes[i9]
            _fcell.setdefault((int(x9 // 60.0), int(y9 // 60.0)),
                              []).append(i9)

        def fallback_at(x, y):
            cx, cy = int(x // 60.0), int(y // 60.0)
            bi, bd = None, 3600.0
            for dx9 in (-1, 0, 1):
                for dy9 in (-1, 0, 1):
                    for i9 in _fcell.get((cx + dx9, cy + dy9), ()):
                        x9, y9 = nodes[i9]
                        d2 = (x9 - x) ** 2 + (y9 - y) ** 2
                        if d2 < bd:
                            bd, bi = d2, i9
            return elev[bi] if bi is not None else None

    # airside union for the proximity-coupling test: the connector must
    # be real taxi surface (the s77 geodesic-seed visibility lesson).
    # Returns the coupling WEIGHT — the straight distance when the
    # connector stays inside pavement; inflated when a small grass notch
    # interrupts it (the surface connects the long way around — CYXY
    # junction #95: lanes 56 m apart, 15 m notch, 6.9 m field cliff);
    # None when the notch dominates (genuinely separate surfaces).
    bridge_test = None
    try:
        from shapely.geometry import LineString as _BLine
        from shapely.ops import unary_union as _bunion
        from shapely.prepared import prep as _bprep
        polys9 = [s.polygon for s in layout.shapes
                  if s.role in PAVEMENT_ROLES
                  and s.polygon is not None and not s.polygon.is_empty]
        if polys9:
            airside_geom9 = _bunion(polys9).buffer(0.5)
            airside9 = _bprep(airside_geom9)

            def bridge_test(pa, pb):
                ln9 = _BLine([pa, pb])
                if airside9.covers(ln9):
                    return ln9.length
                try:
                    out9 = ln9.difference(airside_geom9).length
                except _GEOM_EXC:
                    return None
                if out9 > 0.35 * ln9.length:
                    return None
                return ln9.length + 4.0 * out9
    except _GEOM_EXC:
        bridge_test = None

    # base_hard pins (thresholds, tile-seam pins): the field must not
    # write values the enforce's bands from these pins later reject
    bh_pts = []
    if nodes is not None and base_hard is not None:
        bh_pts = [(nodes[i][0], nodes[i][1], elev[i])
                  for i in range(min(len(nodes), len(base_hard)))
                  if base_hard[i]]
    F = None
    try:
        apt_segs, axis_segs = _corridor_segments(layout, split=True)
        # Ground-vehicle ROAD centerlines (SVC refs) grade at 4 % in
        # the field (service_road_carve.md Step C) — the ramp class
        # must descend to terrain faster than the taxi law allows.
        _svc_lines9 = [e9[0] for e9 in
                       (getattr(layout, "apt_taxi_centerlines", None)
                        or [])
                       if isinstance(e9, (tuple, list)) and len(e9) > 1
                       and str(e9[1]).startswith("SVC")]
        F = _np.build_and_solve(
            apt_segs + axis_segs, rings, TAXI_MAX_GRADE,
            TAXIWAY_MAX_GRADE_CHANGE_PER_M, seed_at=seed_at,
            exit_overrides=exit_overrides, fallback_at=fallback_at,
            bridge_test=bridge_test, n_apt_segments=len(apt_segs),
            extra_band_anchors=bh_pts,
            entry_dist=_interior_entry_dist(layout),
            road_lines=_svc_lines9,
            road_cap=SERVICE_ROAD_MAX_GRADE)
    except _GEOM_EXC:
        F = None
    layout._network_profile_field = F

    n_sampled = n_holes = 0
    for cd in chain_data:
        sts = cd["stations"]
        anchored = cd["anchored"]
        for k, st in enumerate(sts):
            if cd["hard"][k]:
                anchored[k] = True
                continue
            v = None
            gap = float("inf")
            if F is not None and st["mid"] is not None:
                v, gap = F.sample(st["mid"][0], st["mid"][1])
            # mouth mids sit up to ~a half-width off the lane (SPJC #92:
            # one mouth at gap 31 m fell back to its relief value 0.7 m
            # off the field while the facing rect sampled it — a written
            # write-layer wall); beyond ~50 m it is a real coverage hole
            if v is not None and gap <= 50.0:
                cd["elevs"][k] = v
                anchored[k] = True
                n_sampled += 1
            else:
                # field coverage hole: the per-chain flat seed + FAA
                # solve interpolates between the sampled neighbours
                anchored[k] = False
                n_holes += 1
        if not any(anchored):
            anchored[0] = True
            anchored[-1] = True
        cd["st_lo"] = cd["st_hi"] = None

    dem_lo: dict = {}
    dem_hi: dict = {}
    dem_refs: set = set()
    # carry each contact's wanted value to ITS runway's ring vertices
    # ≤60 m (the p10b rule: nearby vertices carry the CONTACT need —
    # adding the contact→vertex leg diluted the T4 demand 107.9→109.6).
    # DIP and RISE both legal (user 2026-06-11: the field demand basis
    # is hard-anchored, so the s73-p9 false-rise class cannot fire).
    if F is not None and nodes is not None:
        for ((x9, y9), wanted, ref9, kind9) in F.demands:
            for i in sorted(rwy_nodes):
                if i >= len(nodes):
                    continue
                if ref9 and rwy_ref_of.get(i, "") != ref9:
                    continue
                xi, yi = nodes[i]
                if (xi - x9) ** 2 + (yi - y9) ** 2 > 3600.0:
                    continue
                if kind9 == "dip" and elev[i] - wanted >= 0.5:
                    dem_hi[i] = min(dem_hi.get(i, float("inf")), wanted)
                    dem_refs.add(rwy_ref_of.get(i, ""))
                elif kind9 == "rise" and wanted - elev[i] >= 0.5:
                    dem_lo[i] = max(dem_lo.get(i, float("-inf")), wanted)
                    dem_refs.add(rwy_ref_of.get(i, ""))
    if _os.environ.get("O4_NPF_DEBUG") == "1":
        if F is None:
            print("[npf] no field (no centerlines or runways)")
        else:
            a9 = F.audit
            print(f"[npf] field nodes={a9['nodes']} edges={a9['edges']} "
                  f"contacts={a9['contacts']} "
                  f"interior={a9['interior_anchors']} "
                  f"components={len(a9['components'])} "
                  f"stations sampled={n_sampled} holes={n_holes}")
            for c9, st9 in a9["components"].items():
                print(f"[npf]   comp {c9}: n={st9['nodes']} "
                      f"len={st9['len_m']:.0f}m "
                      f"contacts={st9['contacts']} "
                      f"refs={st9['refs']} relax={st9['relax']}")
            for d9 in a9["demands"]:
                print(f"[npf]   demand {d9}")
            print(f"[npf] mapped demands: {len(dem_hi)} vertex(es) "
                  f"refs={sorted(dem_refs)}")
    return dem_lo, dem_hi, dem_refs


def _taxi_corridor_profiles(layout, elev, bucket_to_idx, base_hard,
                            nodes=None, coupling=None, dem_ctx=None):
    """Re-profile every taxi CORRIDOR — a chain of taxi rects continuing
    through junctions (same ref, else the best axis-aligned continuation) —
    as ONE smooth 1-D line, exactly like a runway centerline: grade-capped
    (taxi 1.5 %), grade-CHANGE-capped (``TAXIWAY_MAX_GRADE_CHANGE_PER_M``),
    anchored at {hard nodes, runway contacts, corridor termini at their
    current network values}, DEM lowest priority (user 2026-06-10: "the
    priorities for all taxi areas are slope along their taxi route access").

    Why: the solver otherwise settles each shape DEM-near, which is locally
    cap-compliant but lets a corridor V-NOTCH at a junction — HECA's T read
    111.7 → 104.5 (NE mouth) → 105.0 (SW mouth) → 103.5: flat-to-REVERSED
    through junction -10292 where one steady ~1 % ramp exists, and the next
    rect ate the difference (2.5 %).  The profile writes rect ring nodes
    (axial interpolation between mouth stations) and the junction CROSSING
    vertices in the corridor band (interpolated across the gap); junction
    vertices off the corridor stay free, so a junction crossed by two
    corridors blends both.  Returns the written node set — the final
    enforcement holds it, so neighbouring pavement conforms to the corridor
    (taxi routes outrank aprons).  Mutates ``elev``.

    The corridor graph is solved as ONE SYSTEM (the s73 part-5 missing
    piece): everywhere two chains meet a junction — geometric crossing,
    terminus against a crossing run, shared nodes, co-located mouths —
    the shared elevation is a COMMON variable tied across the chains
    (equality at physical points, grade-cap over junction chords), found
    by consensus iteration over each chain's flat-line wish and frozen
    into every profile before the per-chain banded solve.  Without it,
    independent chains flat-seeded between their OWN termini and
    disagreed by metres at shared junctions (#217: 3.9 m / 49 %)."""
    from auto_patch.pavement.runway_segments import faa_joint_solve
    cps = layout.canonical_points

    def _ring_idxs(s):
        ring = _open_ring(list(s.polygon.exterior.coords))
        idxs = [bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
                for x, y in ring]
        return ring, idxs

    rwy_nodes: set = set()
    rwy_ref_of: dict = {}
    for s in layout.shapes:
        if (s.role == ROLE_RUNWAY and s.polygon is not None
                and not s.polygon.is_empty):
            _r, _i = _ring_idxs(s)
            for i in _i:
                if i is not None:
                    rwy_nodes.add(i)
                    rwy_ref_of[i] = s.ref or ""

    # Free-APRON ring nodes: a chain terminus whose mouth opens into an
    # apron (no junction, no rect — ``junc=None``) anchors at the apron's
    # DEM-settled edge value.  That anchor must not VETO the tie network
    # (HECA taxiway A #25: apron-mouth anchor 64.7 froze-out the A5-complex
    # consensus 61.8 and held the 63.3-vs-60.7 junction cliff) — the apron
    # is free pavement that re-grades toward the corridor in the relief
    # re-run, so the corridor profile is the route truth at the seam.
    apron_nodes: set = set()
    apron_sets: list = []        # per-apron ring-node sets (P4 end ties)
    for s in layout.shapes:
        if (s.role == ROLE_APRON and s.polygon is not None
                and not s.polygon.is_empty):
            _r, _i = _ring_idxs(s)
            aset = {i for i in _i if i is not None}
            apron_nodes.update(aset)
            apron_sets.append(aset)

    # ── junctions first (mouth detection is connectivity-driven)
    juncs: list = []
    for s in layout.shapes:
        if (s.role != ROLE_JUNCTION or s.polygon is None
                or s.polygon.is_empty):
            continue
        ring, idxs = _ring_idxs(s)
        juncs.append({"ring": ring, "idxs": idxs,
                      "nodes": {i for i in idxs if i is not None}})

    # ── rect records.  MOUTHS are detected by CONNECTIVITY: the two
    # shared-node clusters against junctions / other corridor rects with the
    # largest separation.  This is the taxi-flow truth — a wide-short
    # connector's taxi axis runs junction→junction across its SHORT
    # dimension (HECA's U: 75 m wide, 12.7 m long; its geometric long axis
    # mis-profiled it at 9.4 % across the taxi direction).  Geometric
    # extremes along ``source_axis`` (or the farthest ring pair) are the
    # fallback when fewer than two port clusters exist.
    raw: list = []
    for s in layout.shapes:
        if (s.role not in _CORRIDOR_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        ring, idxs = _ring_idxs(s)
        if len(ring) < 4:
            continue
        raw.append({"shape": s, "ring": ring, "idxs": idxs,
                    "nodes": {i for i in idxs if i is not None}})
    ports = juncs + raw                       # shapes a mouth can abut
    rects: list = []
    for rr in raw:
        s, ring, idxs = rr["shape"], rr["ring"], rr["idxs"]
        clusters: list = []
        for P in ports:
            if P is rr:
                continue
            sh = rr["nodes"] & P["nodes"]
            if not sh:
                continue
            vk = [k for k, i in enumerate(idxs) if i in sh]
            mx = sum(ring[k][0] for k in vk) / len(vk)
            my = sum(ring[k][1] for k in vk) / len(vk)
            clusters.append({"vk": vk, "mid": (mx, my), "nodes": sh})
        mouths = None
        if len(clusters) >= 2:
            best = (0.0, None)
            for a in range(len(clusters)):
                for b in range(a + 1, len(clusters)):
                    d = math.hypot(
                        clusters[a]["mid"][0] - clusters[b]["mid"][0],
                        clusters[a]["mid"][1] - clusters[b]["mid"][1])
                    if d > best[0]:
                        best = (d, (a, b))
            if best[1] is not None and best[0] >= 8.0:
                mouths = [clusters[best[1][0]], clusters[best[1][1]]]
        if mouths is None:
            # geometric fallback: source_axis, else farthest ring pair
            ux = uy = None
            sa = getattr(s, "source_axis", None)
            if sa is not None:
                try:
                    c0, c1 = sa.coords[0], sa.coords[-1]
                    dx, dy = c1[0] - c0[0], c1[1] - c0[1]
                    al = math.hypot(dx, dy)
                    if al > 1e-6:
                        ux, uy = dx / al, dy / al
                except _GEOM_EXC:
                    pass
            if ux is None:
                bd2, pair = 0.0, None
                for a in range(len(ring)):
                    for b in range(a + 1, len(ring)):
                        d2 = ((ring[b][0] - ring[a][0]) ** 2
                              + (ring[b][1] - ring[a][1]) ** 2)
                        if d2 > bd2:
                            bd2, pair = d2, (a, b)
                if pair is None or bd2 <= 0:
                    continue
                dx = ring[pair[1]][0] - ring[pair[0]][0]
                dy = ring[pair[1]][1] - ring[pair[0]][1]
                al = math.sqrt(bd2)
                ux, uy = dx / al, dy / al
            gp = [x * ux + y * uy for (x, y) in ring]
            pmin, pmax = min(gp), max(gp)
            if pmax - pmin < 8.0:
                continue
            tol = max(2.0, 0.04 * (pmax - pmin))
            mouths = []
            for lo_side in (True, False):
                if lo_side:
                    vk = [k for k, p in enumerate(gp) if p <= pmin + tol]
                else:
                    vk = [k for k, p in enumerate(gp) if p >= pmax - tol]
                if not vk:
                    mouths = None
                    break
                mx = sum(ring[k][0] for k in vk) / len(vk)
                my = sum(ring[k][1] for k in vk) / len(vk)
                mouths.append({
                    "vk": vk, "mid": (mx, my),
                    "nodes": {idxs[k] for k in vk
                              if idxs[k] is not None}})
            if not mouths:
                continue
        # axis = mouth A → mouth B; axial projections for body writeback
        ax, ay = mouths[0]["mid"]
        bx, by = mouths[1]["mid"]
        dx, dy = bx - ax, by - ay
        span = math.hypot(dx, dy)
        if span < 8.0:
            continue
        ux, uy = dx / span, dy / span
        projs = [x * ux + y * uy for (x, y) in ring]
        for m in mouths:
            m["p"] = m["mid"][0] * ux + m["mid"][1] * uy
            w = 0.0
            for a in range(len(m["vk"])):
                for b in range(a + 1, len(m["vk"])):
                    w = max(w, math.hypot(
                        ring[m["vk"][a]][0] - ring[m["vk"][b]][0],
                        ring[m["vk"][a]][1] - ring[m["vk"][b]][1]))
            m["halfw"] = max(w / 2.0, 4.0)
        rects.append({"shape": s, "ring": ring, "idxs": idxs,
                      "projs": projs, "span": span, "mouths": mouths})

    if _os.environ.get("O4_CORR_CHDBG") == "1":
        have9 = {id(r["shape"]) for r in rects}
        drop9 = sorted((rr["shape"].ref or "?", round(rr["shape"].polygon.area))
                       for rr in raw if id(rr["shape"]) not in have9)
        print(f"[chdbg] raw={len(raw)} rects={len(rects)} "
              f"mouth-dropped={drop9}")
    if not rects:
        return set()
    for r in rects:
        for m in r["mouths"]:
            m["junc"] = next((ji for ji, J in enumerate(juncs)
                              if m["nodes"] & J["nodes"]), None)
    j_mouths: dict = {}
    for ri, r in enumerate(rects):
        for mi, m in enumerate(r["mouths"]):
            if m["junc"] is not None:
                j_mouths.setdefault(m["junc"], []).append((ri, mi))

    used = [False] * len(rects)

    def _out_dir(ri, mi):
        r = rects[ri]
        ma, mb = r["mouths"][mi], r["mouths"][1 - mi]
        dx, dy = ma["mid"][0] - mb["mid"][0], ma["mid"][1] - mb["mid"][1]
        al = math.hypot(dx, dy) or 1.0
        return dx / al, dy / al

    def _is_wide(ri):
        # A wide-short connector (HECA's U: 12.7 m long, 75 m wide between
        # two junctions) has a NOISY mouth-mid axis — the lateral offsets of
        # its junction contacts dominate the 12 m direction.  Its axis must
        # not gate continuations; the chain's travel direction carries.
        r = rects[ri]
        return r["span"] < max(mm["halfw"] for mm in r["mouths"])

    def _continuation(ri, mi, same_ref_only, tdir=None):
        r = rects[ri]
        m = r["mouths"][mi]
        out = (tdir if (tdir is not None and _is_wide(ri))
               else _out_dir(ri, mi))
        cands = set()
        if m["junc"] is not None:
            cands.update((rj, mj) for (rj, mj) in j_mouths[m["junc"]]
                         if rj != ri)
        for rj, r2 in enumerate(rects):
            if rj == ri:
                continue
            for mj, m2 in enumerate(r2["mouths"]):
                if m["nodes"] & m2["nodes"]:
                    cands.add((rj, mj))
        best = None
        for rj, mj in cands:
            if used[rj]:
                continue
            same_ref = bool(rects[ri]["shape"].ref) and (
                (rects[ri]["shape"].ref or "")
                == (rects[rj]["shape"].ref or ""))
            if same_ref_only and not same_ref:
                continue
            m2 = rects[rj]["mouths"][mj]
            if _is_wide(rj):
                cos_a = 1.0                   # candidate axis unreliable
            else:
                inward = _out_dir(rj, 1 - mj)  # direction INTO rj at mouth
                cos_a = out[0] * inward[0] + out[1] * inward[1]
            gx, gy = m2["mid"][0] - m["mid"][0], m2["mid"][1] - m["mid"][1]
            gl = math.hypot(gx, gy)
            cos_g = ((out[0] * gx + out[1] * gy) / gl) if gl > 2.0 else 1.0
            if cos_a < _CORRIDOR_COS_MIN or cos_g < _CORRIDOR_COS_MIN:
                continue
            score = (1 if same_ref else 0, cos_a + cos_g)
            if best is None or score > best[0]:
                best = (score, rj, mj)
        return best

    # TWO-PHASE CHAINING: same-ref chains bind FIRST (else a crossing
    # corridor's greedy walk steals a shared connector — HECA's G grabbed
    # T's south piece, breaking T at the junction and re-anchoring its
    # mouth at the bad value), then chain ends extend with the best
    # axis-aligned cross-ref continuation (T4 continues into U).
    def _walk(chain, at_end, same_ref_only):
        if at_end:
            ri, _nmi, mi = chain[-1]
        else:
            ri, mi, _fmi = chain[0]
        # Travel direction at the chain end: the WHOLE-chain end-to-end
        # vector when the chain has extent (robust when the end element is
        # a wide-short connector whose own axis is noise), else the end
        # rect's axis.
        tdir = None
        if len(chain) > 1:
            h = rects[chain[0][0]]["mouths"][chain[0][1]]["mid"]
            t = rects[chain[-1][0]]["mouths"][chain[-1][2]]["mid"]
            dx, dy = ((t[0] - h[0], t[1] - h[1]) if at_end
                      else (h[0] - t[0], h[1] - t[1]))
            gl = math.hypot(dx, dy)
            if gl > 10.0:
                tdir = (dx / gl, dy / gl)
        if tdir is None and not _is_wide(ri):
            tdir = _out_dir(ri, mi)
        while True:
            nxt = _continuation(ri, mi, same_ref_only, tdir)
            if nxt is None:
                break
            _sc, rj, mj = nxt
            used[rj] = True
            if at_end:
                chain.append((rj, mj, 1 - mj))
            else:
                chain.appendleft((rj, 1 - mj, mj))
            ri, mi = rj, 1 - mj
            if not _is_wide(rj):
                tdir = _out_dir(rj, mi)

    chains: list = []
    for seed in sorted(range(len(rects)), key=lambda k: -rects[k]["span"]):
        if used[seed]:
            continue
        used[seed] = True
        chain = deque([(seed, 0, 1)])         # (rect, near_mouth, far_mouth)
        _walk(chain, True, True)
        _walk(chain, False, True)
        chains.append(list(chain))

    # ── phase B: MERGE chains end-to-end across refs.  Every rect was
    # consumed as a phase-A seed (each unchained rect is its own singleton
    # chain), so cross-ref continuation candidates are CHAIN ENDS, not free
    # rects — T4's stub chain merges into the U chain across junction
    # -10292, giving the "coming off T4 into U" continuity.
    def _flip(c):
        return [(ri, f, n) for (ri, n, f) in reversed(c)]

    def _end_info(c, tail):
        """(mouth, travel-out-direction) at the chain's tail/head."""
        if tail:
            ri, _n, mi = c[-1]
        else:
            ri, mi, _f = c[0]
        m = rects[ri]["mouths"][mi]
        h = rects[c[0][0]]["mouths"][c[0][1]]["mid"]
        t = rects[c[-1][0]]["mouths"][c[-1][2]]["mid"]
        dx, dy = ((t[0] - h[0], t[1] - h[1]) if tail
                  else (h[0] - t[0], h[1] - t[1]))
        gl = math.hypot(dx, dy)
        if gl > 10.0:
            out = (dx / gl, dy / gl)
        elif not _is_wide(ri):
            out = _out_dir(ri, mi)
        else:
            out = None
        return m, out

    merged_any = True
    while merged_any:
        merged_any = False
        for ci in range(len(chains)):
            if chains[ci] is None:
                continue
            for tail in (True, False):
                m, out = _end_info(chains[ci], tail)
                best = None
                for cj in range(len(chains)):
                    if cj == ci or chains[cj] is None:
                        continue
                    for j_tail in (False, True):
                        m2, inw_rev = _end_info(chains[cj], j_tail)
                        # adjacency: shared junction or shared nodes
                        if not ((m["junc"] is not None
                                 and m["junc"] == m2["junc"])
                                or (m["nodes"] & m2["nodes"])):
                            continue
                        # cj's inward travel = reverse of its outward
                        # direction at the joining end
                        inward = (None if inw_rev is None
                                  else (-inw_rev[0], -inw_rev[1]))
                        cos_a = 1.0
                        if out is not None and inward is not None:
                            cos_a = (out[0] * inward[0]
                                     + out[1] * inward[1])
                        gx = m2["mid"][0] - m["mid"][0]
                        gy = m2["mid"][1] - m["mid"][1]
                        gl = math.hypot(gx, gy)
                        cos_g = 1.0
                        if out is not None and gl > 2.0:
                            cos_g = (out[0] * gx + out[1] * gy) / gl
                        if (cos_a < _CORRIDOR_COS_MIN
                                or cos_g < _CORRIDOR_COS_MIN):
                            continue
                        score = cos_a + cos_g
                        if best is None or score > best[0]:
                            best = (score, cj, j_tail)
                if best is None:
                    continue
                _sc, cj, j_tail = best
                # Cross-ref merges only where a STUB or wide-short
                # connector carries the continuation (T4's stub into U).
                # Merging two arbitrary refs (R+R1, S+E, TX18+TX17)
                # imposes one flat-seed ramp across unrelated taxiways —
                # multi-metre profile conflicts at the shared junctions
                # (#217 read 3.9 m / 49 %).
                eri = (chains[ci][-1][0] if tail else chains[ci][0][0])
                erj = (chains[cj][-1][0] if j_tail else chains[cj][0][0])
                # SAME-REF ends always merge (one physical taxiway split
                # by a junction the phase-A local gate rejected): HECA's
                # two 'A' chains sat at 59.2 and 64.1 across one junction
                # — a 4.3 m disagreement where ONE profile ramps <1 %.
                # The #217 lesson (no arbitrary cross-ref ramps) applies
                # to DIFFERENT refs only.
                same_ref9 = bool(rects[eri]["shape"].ref) and (
                    rects[eri]["shape"].ref == rects[erj]["shape"].ref)
                bridge_ok = same_ref9 or any(
                    rects[rr]["shape"].role == ROLE_STUB or _is_wide(rr)
                    for rr in (eri, erj))
                if not bridge_ok:
                    continue
                other = chains[cj]
                if tail and not j_tail:        # my tail + their head
                    chains[ci] = chains[ci] + other
                elif tail and j_tail:          # my tail + their tail
                    chains[ci] = chains[ci] + _flip(other)
                elif not tail and j_tail:      # my head + their tail
                    chains[ci] = other + chains[ci]
                else:                          # my head + their head
                    chains[ci] = _flip(other) + chains[ci]
                chains[cj] = None
                merged_any = True
    chains = [c for c in chains if c is not None]
    if _os.environ.get("O4_CORRIDOR_DEBUG") == "1":
        for c in chains:
            if len(c) >= 2:
                refs9 = [(rects[ri]["shape"].ref or "?") for (ri, _n, _f) in c]
                hm = rects[c[0][0]]["mouths"][c[0][1]]
                tm = rects[c[-1][0]]["mouths"][c[-1][2]]
                print(f"[corr] chain-ends {refs9} "
                      f"head junc={hm['junc']} mid={tuple(round(v) for v in hm['mid'])} "
                      f"tail junc={tm['junc']} mid={tuple(round(v) for v in tm['mid'])}")
        for c in chains:
            if len(c) == 1:
                ri = c[0][0]
                r = rects[ri]
                mids = [tuple(round(v) for v in m["mid"])
                        for m in r["mouths"]]
                jcs = [m["junc"] for m in r["mouths"]]
                print(f"[corr] singleton ref={r['shape'].ref or '?'} "
                      f"span={r['span']:.0f} mouths@{mids} junc={jcs}")
    # multi-rect chains always profile; a SINGLETON survives when a mouth
    # junction touches the RUNWAY — the runway-exit extension below gives
    # it a hard runway anchor so the corridor WRITES the climb through
    # the exit junction (HECA A4/A5: feasibility headroom alone never
    # lifted the projected surface — it needs a driver)
    def _touches_runway(c):
        for mi2 in (c[0][1], c[0][2]):
            ji2 = rects[c[0][0]]["mouths"][mi2].get("junc")
            if ji2 is not None and (juncs[ji2]["nodes"] & rwy_nodes):
                return True
        return False

    if _os.environ.get("O4_CORRIDOR_DEBUG") == "1":
        for c in chains:
            if len(c) == 1:
                jt = _touches_runway(c)
                print(f"[corr] singleton keep={jt} "
                      f"ref={rects[c[0][0]]['shape'].ref or '?'} "
                      f"juncs={[rects[c[0][0]]['mouths'][m].get('junc') for m in (0, 1)]}")
    # NOTE (s77p3 measured, then REVERTED): profiling ALL singletons (not
    # just runway-touching) closed #256 fully and restored A5 ≈ 60.1, but
    # the tie network distributed the C/B-vs-S route squeeze onto MORE
    # surfaces — standalone >5 % walls went 21 → 46 (writes) / 29
    # (tie-only).  Grading the through-apron connectors (HECA taxiway B,
    # user request) needs per-tie handling of route-pinned-apart mouth
    # networks first — the open design item.
    # Under the NETWORK PROFILE MODEL the gate lifts: singleton values are
    # SAMPLES of one network-wide field (no tie network exists to spread a
    # squeeze), and the through-apron connectors grade from the field —
    # the s77p3 design item this model subsumes.
    if not NETWORK_PROFILE_MODEL:
        chains = [c for c in chains if len(c) >= 2 or _touches_runway(c)]

    # ── ROUTE BANDS (route-field model, user 2026-06-10 "correct grade is
    # king, DEM is a starting point"): per-node feasible band from the
    # runway anchors measured along the taxi-route graph.  Corridor
    # profiles must THREAD these bands — T's flat seed otherwise ignores
    # the 05C-route demand entering the shared junction via T4, and the
    # enforce's route bands then fight the held corridor (the 64 % cliffs).
    reach_lo = reach_hi = None
    if nodes is not None and rwy_nodes and not NETWORK_PROFILE_MODEL:
        # (network profile model: stations sample the solved field —
        # the station route bands exist only to thread the tie layer)
        try:
            reach_lo, reach_hi = _runway_reach_bands(
                nodes, elev, rwy_nodes, set(), [], TAXI_MAX_GRADE, layout,
                entry_dist=_interior_entry_dist(layout))
        except _GEOM_EXC:
            reach_lo = reach_hi = None

    from shapely.geometry import LineString as _LineString
    from shapely.geometry import Polygon as _Polygon

    jpoly_cache: dict = {}
    chord_cache: dict = {}

    def _junc_chord_ok(ji, pa, pb):
        ka = (round(pa[0], 1), round(pa[1], 1))
        kb = (round(pb[0], 1), round(pb[1], 1))
        key = (ji, ka, kb) if ka <= kb else (ji, kb, ka)
        hit = chord_cache.get(key)
        if hit is not None:
            return hit
        poly = jpoly_cache.get(ji)
        if poly is None:
            try:
                poly = _Polygon(juncs[ji]["ring"]).buffer(1.0)
            except _GEOM_EXC:
                poly = False
            jpoly_cache[ji] = poly
        if poly is False:
            ok = False
        else:
            try:
                ok = bool(poly.covers(_LineString([pa, pb])))
            except _GEOM_EXC:
                ok = False
        chord_cache[key] = ok
        return ok

    jgeo_cache: dict = {}

    def _junc_geo_table(ji):
        """All-pairs in-polygon geodesic distances between ring vertices
        of junction ji (Dijkstra over the ring-vertex visibility graph)."""
        tab = jgeo_cache.get(ji)
        if tab is not None:
            return tab
        ring = juncs[ji]["ring"]
        m = len(ring)
        adj: list = [[] for _ in range(m)]
        for a in range(m):
            for b in range(a + 1, m):
                if _junc_chord_ok(ji, ring[a], ring[b]):
                    dd = math.hypot(ring[a][0] - ring[b][0],
                                    ring[a][1] - ring[b][1])
                    adj[a].append((b, dd))
                    adj[b].append((a, dd))
        tab = []
        for s in range(m):
            dist = [float("inf")] * m
            dist[s] = 0.0
            pq = [(0.0, s)]
            while pq:
                dc, u = heapq.heappop(pq)
                if dc > dist[u] + 1e-9:
                    continue
                for v2, w2 in adj[u]:
                    nd = dc + w2
                    if nd < dist[v2] - 1e-9:
                        dist[v2] = nd
                        heapq.heappush(pq, (nd, v2))
            tab.append(dist)
        jgeo_cache[ji] = tab
        return tab

    jaxes_cache: dict = {}

    def _junc_axis_arc(ji, pa, pb):
        """Longest along-axis ARC between pa and pb when both lie within
        the perp tolerance of a centerline axis through junction ji —
        the curve-aware grade distance (user 2026-06-10: the cap applies
        along the CENTERLINE; a straight chord under-measures a turning
        route, pinning the junction flat)."""
        ax_list = jaxes_cache.get(ji)
        if ax_list is None:
            try:
                ax_list = _collect_junction_axes(
                    layout, _Polygon(juncs[ji]["ring"]))
            except _GEOM_EXC:
                ax_list = []
            jaxes_cache[ji] = ax_list
        if not ax_list:
            return None
        from shapely.geometry import Point as _Point
        Pa, Pb = _Point(pa), _Point(pb)
        best = None
        for ax in ax_list:
            try:
                if (ax.distance(Pa) <= JUNCTION_AXIS_PERP_TOL_M
                        and ax.distance(Pb)
                        <= JUNCTION_AXIS_PERP_TOL_M):
                    arc = abs(ax.project(Pa) - ax.project(Pb))
                    if best is None or arc > best:
                        best = arc
            except _GEOM_EXC:
                continue
        return best

    def _junc_geo_dist(ji, pa, pb):
        """In-junction grade-path length pa→pb: the direct chord when it
        stays inside the junction, else the SHORTEST multi-bend path over
        the ring-vertex visibility graph.  An L-shaped junction still
        constrains through its interior, and a one-bend approximation
        OVER-estimates around double corners — two vertices clamped at
        one-bend caps then violate their mutual chord (CYXY #74 read
        2.16 % between two twist writes both 'at cap').  When both points
        follow a centerline AXIS through the junction, the along-axis ARC
        supersedes a shorter straight path (curve-aware grading — the
        route turns, the chord does not).  ``None`` = no in-junction
        path found."""
        if _junc_chord_ok(ji, pa, pb):
            d = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
            arc = _junc_axis_arc(ji, pa, pb)
            return max(d, arc) if arc is not None else d
        ring = juncs[ji]["ring"]
        m = len(ring)
        tab = _junc_geo_table(ji)

        def _reach(p):
            for k2, q in enumerate(ring):
                if (abs(q[0] - p[0]) < 1e-6
                        and abs(q[1] - p[1]) < 1e-6):
                    return tab[k2]
            base = [(math.hypot(p[0] - R[0], p[1] - R[1])
                     if _junc_chord_ok(ji, p, R) else float("inf"))
                    for R in ring]
            return [min(base[r1] + tab[r1][k2] for r1 in range(m))
                    for k2 in range(m)]

        ra = _reach(pa)
        rb = _reach(pb)
        best = min((a + b for a, b in zip(ra, rb)),
                   default=float("inf"))
        if best >= float("inf"):
            return None
        arc = _junc_axis_arc(ji, pa, pb)
        return max(best, arc) if arc is not None else best

    # ── STAGE A: stations per chain (geometry + current network values).
    # Profiles are NOT solved here — solving chains one at a time let
    # INDEPENDENT chains disagree by metres at SHARED junctions (#217 read
    # 3.9 m: each chain flat-seeded between its OWN termini, and crossing
    # reconciliation was sequential first-writer-wins).  The JOINT
    # corridor-network solve below treats every shared-junction elevation
    # as ONE common variable across all chains.
    if _os.environ.get("O4_CORR_CHDBG") == "1":
        cov9: set = set()
        for ch9 in chains:
            if not ch9:
                continue
            cov9.update(ri9 for (ri9, _n9, _f9) in ch9)
        miss9 = sorted({rects[ri9]["shape"].ref or "?"
                        for ri9 in range(len(rects)) if ri9 not in cov9})
        bch9 = [sorted({rects[ri9]["shape"].ref or "?"
                        for (ri9, _n, _f) in ch9})
                for ch9 in chains if ch9
                and any((rects[ri9]["shape"].ref or "") == "B"
                        for (ri9, _n, _f) in ch9)]
        print(f"[chdbg] chains={sum(1 for c in chains if c)} "
              f"uncovered-refs={miss9} B-chains={bch9}")
    chain_data: list = []
    for chain in chains:
        stations: list = []
        mouth_st: list = []                   # per chain elem: [k_near, k_far]
        gaps: list = []                       # junction-gap crossing segments
        d = 0.0
        prev_mid = None
        prev_m = None
        for (ri, nmi, fmi) in chain:
            r = rects[ri]
            pair = []
            for mi2 in (nmi, fmi):
                m = r["mouths"][mi2]
                if prev_mid is not None:
                    gl = math.hypot(m["mid"][0] - prev_mid[0],
                                    m["mid"][1] - prev_mid[1])
                    # record the junction gap segment (entering a rect) —
                    # the tie pass below intersects these ACROSS chains
                    ji = prev_m.get("junc") if prev_m else None
                    if (mi2 == nmi and gl > 2.0 and ji is not None
                            and ji == m.get("junc")):
                        gaps.append({
                            "ji": ji, "A": prev_mid, "B": m["mid"],
                            "dA": d, "dB": d + gl,
                            "band": max(prev_m["halfw"],
                                        m["halfw"]) + 2.0})
                    d += gl
                if stations and d - stations[-1]["d"] < 1.0:
                    stations[-1]["nodes"] |= m["nodes"]   # abutting mouths
                    if stations[-1]["junc"] is None:
                        stations[-1]["junc"] = m.get("junc")
                    pair.append(len(stations) - 1)
                else:
                    stations.append({"d": d, "nodes": set(m["nodes"]),
                                     "mid": m["mid"], "halfw": m["halfw"],
                                     "junc": m.get("junc")})
                    pair.append(len(stations) - 1)
                prev_mid = m["mid"]
                prev_m = m
            mouth_st.append(pair)
        L = stations[-1]["d"]
        if L < 8.0 or len(stations) < 2:
            continue                  # final ≥3-station/30 m gate is below
        elevs = []
        hard = []
        for st in stations:
            vals = [elev[i] for i in st["nodes"]]
            elevs.append(sum(vals) / len(vals))
            hard.append(any(base_hard[i] for i in st["nodes"])
                        or bool(st["nodes"] & rwy_nodes))
        chain_data.append({
            "chain": chain, "stations": stations, "mouth_st": mouth_st,
            "gaps": gaps, "L": L, "elevs": elevs, "hard": hard,
            "anchored": [h or k == 0 or k == len(stations) - 1
                         for k, h in enumerate(hard)]})

    # ── RUNWAY-EXIT EXTENSION (user 2026-06-10: a high-speed exit
    # junction must CARRY the climb from the runway to its rect — HECA
    # #282 should rise ~1.8 m along its curved centerline run, and
    # feasibility headroom alone never lifts a projected surface).  A
    # chain terminus whose mouth junction touches the RUNWAY gains a
    # virtual HARD station at the nearest runway vertex, at the
    # curve-aware in-junction distance; the corridor profile then ramps
    # from the runway value through the junction, and the recorded
    # crossing segment lets the twist pass paint the junction interior.
    exit_overrides: list = []         # (mouth_xy, contact_xy, arc, value)
    for cd in chain_data:
        sts = cd["stations"]
        for end in (0, 1):
            k = 0 if end == 0 else len(sts) - 1
            st = sts[k]
            ji = st.get("junc")
            if _os.environ.get("O4_CORRIDOR_DEBUG") == "1" \
                    and len(cd["chain"]) == 1:
                rr5 = rects[cd["chain"][0][0]]["shape"].ref or "?"
                rw5 = (len(juncs[ji]["nodes"] & rwy_nodes)
                       if ji is not None else -1)
                ax5 = jaxes_cache.get(ji)
                print(f"[corr]   ext-eval ref={rr5} end={end} ji={ji} "
                      f"hard={cd['hard'][k] if cd['hard'] else '?'} "
                      f"mid={st['mid'] is not None} rwyN={rw5} "
                      f"axes={'?' if ax5 is None else len(ax5)}")
            if (ji is None or st["mid"] is None
                    or not cd["hard"] or cd["hard"][k]):
                continue
            # the contact point and the climb distance come from the EXIT
            # CENTERLINE AXIS, not the nearest runway vertex: the nearest
            # vertex sits a short geodesic away across the fan (47 m at
            # HECA #282) while the curved centerline runs ~120 m — using
            # the short-cut dragged A4's apron end DOWN to runway+0.7
            # instead of letting the junction carry the climb.
            ax_list = jaxes_cache.get(ji)
            if ax_list is None:
                try:
                    ax_list = _collect_junction_axes(
                        layout, _Polygon(juncs[ji]["ring"]))
                except _GEOM_EXC:
                    ax_list = []
                jaxes_cache[ji] = ax_list
            from shapely.geometry import Point as _Pt2
            Pm = _Pt2(st["mid"])
            try:
                jp3 = _Polygon(juncs[ji]["ring"])
            except _GEOM_EXC:
                continue
            rwys3 = [s3 for s3 in layout.shapes
                     if s3.role == ROLE_RUNWAY and s3.polygon is not None
                     and not s3.polygon.is_empty
                     and jp3.distance(s3.polygon) <= 3.0]
            # Per qualifying axis: contact = its NEAREST runway
            # intersection (extending the cut end straight when the
            # ingest dropped the runway-crossing part); across axes take
            # the LONGEST arc — the curved high-speed exit line is the
            # physical taxi path, while the rect's own short stub line
            # extended straight under-counts the climb (A4 read 40 m
            # instead of ~120 and the relax dragged its apron end down).
            best = None
            n_ax = 0
            for ax3 in ax_list:
                try:
                    if ax3.distance(Pm) > JUNCTION_AXIS_PERP_TOL_M:
                        continue
                except _GEOM_EXC:
                    continue
                n_ax += 1
                t_m = ax3.project(Pm)
                acs3 = list(ax3.coords)
                ax_hit = None
                for s3 in rwys3:
                    try:
                        inter = ax3.intersection(s3.polygon.exterior)
                    except _GEOM_EXC:
                        continue
                    pts3 = []
                    if not inter.is_empty:
                        pts3 = ([inter] if inter.geom_type == "Point"
                                else [g for g in
                                      getattr(inter, "geoms", ())
                                      if g.geom_type == "Point"])
                    for p3 in pts3:
                        arc3 = abs(ax3.project(p3) - t_m)
                        if arc3 > 4.0 and (ax_hit is None
                                           or arc3 < ax_hit[0]):
                            ax_hit = (arc3, (p3.x, p3.y), s3)
                    if pts3 or len(acs3) < 2:
                        continue
                    for (p_end, p_prev, end_t) in (
                            (acs3[0], acs3[1], 0.0),
                            (acs3[-1], acs3[-2], ax3.length)):
                        dxe = p_end[0] - p_prev[0]
                        dye = p_end[1] - p_prev[1]
                        dl3 = math.hypot(dxe, dye)
                        if dl3 < 1e-6:
                            continue
                        ray = _LineString([
                            p_end,
                            (p_end[0] + dxe / dl3 * 250.0,
                             p_end[1] + dye / dl3 * 250.0)])
                        try:
                            hit = ray.intersection(s3.polygon.exterior)
                        except _GEOM_EXC:
                            continue
                        if hit.is_empty:
                            continue
                        hpts = ([hit] if hit.geom_type == "Point"
                                else [g for g in
                                      getattr(hit, "geoms", ())
                                      if g.geom_type == "Point"])
                        for p3 in hpts:
                            arc3 = abs(end_t - t_m) + ray.project(p3)
                            if arc3 > 4.0 and (ax_hit is None
                                               or arc3 < ax_hit[0]):
                                ax_hit = (arc3, (p3.x, p3.y), s3)
                if ax_hit is not None and (best is None
                                           or ax_hit[0] > best[0]):
                    best = ax_hit
            # the apt.dat line often CUTS the fan straight across (A4's
            # line: 40 m in-junction where the flow runs ~120-190 m) —
            # the fan polygon itself records the flow: its far
            # runway-adjacent THROAT vertex by in-junction geodesic is
            # the entry; take the longest of the two measures.  ONLY for
            # TANGENTIAL entries (high-speed exits): a chain entering
            # PERPENDICULAR (T4 straight into the 05C junction) must not
            # use the throat — the farthest runway vertex of a
            # runway-hugging junction is just runway length, and the
            # inflated distance ATE the runway-flex demand (05C dip
            # regressed 107.9 → 110.4).
            el0 = cd["chain"][0] if end == 0 else cd["chain"][-1]
            tmi = el0[1] if end == 0 else el0[2]
            tdir = _out_dir(el0[0], tmi)
            tangential = False
            for s3 in rwys3:
                rc3 = _open_ring(list(s3.polygon.exterior.coords))
                if len(rc3) != 4:
                    continue
                e1 = math.hypot(rc3[1][0] - rc3[0][0],
                                rc3[1][1] - rc3[0][1])
                e2 = math.hypot(rc3[2][0] - rc3[1][0],
                                rc3[2][1] - rc3[1][1])
                if e1 >= e2 and e1 > 1e-6:
                    axd = ((rc3[1][0] - rc3[0][0]) / e1,
                           (rc3[1][1] - rc3[0][1]) / e1)
                elif e2 > 1e-6:
                    axd = ((rc3[2][0] - rc3[1][0]) / e2,
                           (rc3[2][1] - rc3[1][1]) / e2)
                else:
                    continue
                if abs(tdir[0] * axd[0] + tdir[1] * axd[1]) >= 0.5:
                    tangential = True
                    break
            if tangential and len(cd["chain"]) == 1:
                # exit-fan THROAT only for SINGLETON stubs: a multi-rect
                # through-corridor (T4+U) has real centerline data and a
                # near contact — the 216 m throat budget dissolved its
                # runway-flex demand (05C dip 107.9 → 110.4)
                for k2, i3 in enumerate(juncs[ji]["idxs"]):
                    if i3 is None or i3 not in rwy_nodes:
                        continue
                    rp = juncs[ji]["ring"][k2]
                    gd6 = _junc_geo_dist(ji, st["mid"], rp)
                    if gd6 is None or gd6 <= 4.0:
                        continue
                    if best is None or gd6 > best[0]:
                        best = (gd6, rp, i3)
            if n_ax == 0:
                if (_os.environ.get("O4_CORRIDOR_DEBUG") == "1"
                        and juncs[ji]["nodes"] & rwy_nodes):
                    print(f"[corr]   exit-ext SKIP (no axis ≤"
                          f"{JUNCTION_AXIS_PERP_TOL_M:.0f}m of mouth; "
                          f"{len(ax_list)} axes) junc={ji} "
                          f"mouth={tuple(round(v) for v in st['mid'])}")
                continue
            if best is None:
                if (_os.environ.get("O4_CORRIDOR_DEBUG") == "1"
                        and juncs[ji]["nodes"] & rwy_nodes):
                    print(f"[corr]   exit-ext SKIP (axis has no runway "
                          f"intersection >4m) junc={ji}")
                continue
            gd, hp, s3b = best
            if _os.environ.get("O4_CORRIDOR_DEBUG") == "1":
                refs6 = sorted({rects[r6]["shape"].ref or "?"
                                for (r6, _n6, _f6) in cd["chain"]})
                print(f"[corr]   exit-ext refs={refs6} end={end} ji={ji} "
                      f"tang={tangential} tdir=({tdir[0]:+.2f},"
                      f"{tdir[1]:+.2f}) gd={gd:.0f} "
                      f"contact=({hp[0]:.0f},{hp[1]:.0f}) "
                      f"vertex={not hasattr(s3b, 'polygon')}")
            if not hasattr(s3b, "polygon"):      # throat VERTEX contact
                i6 = s3b
                val5 = elev[i6]
                s3b = None
            # runway surface value AT the contact: interpolate the ring
            # edge containing it (runway corners are sparse — requiring
            # a vertex within 10 m silently skipped most exits)
            if s3b is None:
                pass
            else:
                val5 = None
                bseg = 5.0
                rring = _open_ring(list(s3b.polygon.exterior.coords))
                m5 = len(rring)
                for k2 in range(m5):
                    a5 = rring[k2]
                    b5 = rring[(k2 + 1) % m5]
                    ex5, ey5 = b5[0] - a5[0], b5[1] - a5[1]
                    L5sq = ex5 * ex5 + ey5 * ey5
                    if L5sq < 1e-9:
                        continue
                    t5 = (((hp[0] - a5[0]) * ex5
                           + (hp[1] - a5[1]) * ey5) / L5sq)
                    if not -0.05 <= t5 <= 1.05:
                        continue
                    t5c = min(max(t5, 0.0), 1.0)
                    px5 = a5[0] + t5c * ex5
                    py5 = a5[1] + t5c * ey5
                    d5 = math.hypot(hp[0] - px5, hp[1] - py5)
                    if d5 >= bseg:
                        continue
                    ia5 = bucket_to_idx.get(
                        cps.get_or_add(float(a5[0]), float(a5[1])))
                    ib5 = bucket_to_idx.get(
                        cps.get_or_add(float(b5[0]), float(b5[1])))
                    if ia5 is None or ib5 is None:
                        continue
                    bseg = d5
                    val5 = elev[ia5] + t5c * (elev[ib5] - elev[ia5])
            if val5 is None:
                if _os.environ.get("O4_CORRIDOR_DEBUG") == "1":
                    print(f"[corr]   exit-ext SKIP (no runway edge at "
                          f"contact) junc={ji}")
                continue
            band_v = max(st["halfw"], 8.0) + 2.0
            # the measured curve-aware exit (arc + contact value) is also
            # the network-field's contact for this fan — promoted into
            # the graph as an arc-weighted edge (design prereq 1: the
            # apt.dat line cuts the fan corner)
            if s3b is not None:
                ref5 = s3b.ref or ""
            else:                  # throat-vertex contact (i6 set above)
                ref5 = rwy_ref_of.get(i6, "")
            exit_overrides.append((st["mid"], hp, gd, val5, ref5))
            vst = {"d": 0.0, "nodes": set(), "mid": hp, "halfw": 4.0,
                   "junc": ji, "virtual": True}
            if end == 0:
                for s2 in sts:
                    s2["d"] += gd
                for g2 in cd["gaps"]:
                    g2["dA"] += gd
                    g2["dB"] += gd
                sts.insert(0, vst)
                cd["elevs"].insert(0, val5)
                cd["hard"].insert(0, True)
                cd["mouth_st"] = [[a3 + 1, b3 + 1]
                                  for (a3, b3) in cd["mouth_st"]]
                cd["gaps"].insert(0, {
                    "ji": ji, "A": hp, "B": st["mid"],
                    "dA": 0.0, "dB": gd, "band": band_v})
            else:
                vst["d"] = sts[-1]["d"] + gd
                cd["gaps"].append({
                    "ji": ji, "A": st["mid"], "B": hp,
                    "dA": sts[-1]["d"], "dB": vst["d"],
                    "band": band_v})
                sts.append(vst)
                cd["elevs"].append(val5)
                cd["hard"].append(True)
        cd["L"] = sts[-1]["d"]
        cd["anchored"] = [h or k3 == 0 or k3 == len(sts) - 1
                          for k3, h in enumerate(cd["hard"])]
        # an extended SINGLETON's far terminus anchors at the NEIGHBOUR-
        # HOOD it must meet, not at its own nodes: the relief carves a
        # low channel through the far junction along the stub's old
        # descent (HECA A5: terminus read 60.5 while junction #284's
        # surroundings sit at 64.4 — the chain then ramped into its own
        # carve and the 3.7 m cliff stayed)
        if (len(cd["chain"]) == 1
                and any(s7.get("virtual") for s7 in sts)):
            own7 = set()
            for s7 in sts:
                own7 |= s7["nodes"]
            for k7 in (0, len(sts) - 1):
                if sts[k7].get("virtual") or cd["hard"][k7]:
                    continue
                ji7 = sts[k7].get("junc")
                if ji7 is None:
                    continue
                vals7 = sorted(
                    elev[i7] for i7 in juncs[ji7]["nodes"] - own7
                    if not base_hard[i7])
                if len(vals7) >= 3:
                    # the carve runs THROUGH the junction, so even its
                    # median reads carved (A5/#284: median 60.5 vs the
                    # uncarved side 64.4) — meet the HIGH side, clamped
                    # to what the chain can legally climb from the
                    # runway virtual
                    vv7 = vals7[-1]
                    for s8 in sts:
                        if s8.get("virtual"):
                            lim8 = (cd["elevs"][sts.index(s8)]
                                    + TAXI_MAX_GRADE
                                    * abs(sts[k7]["d"] - s8["d"]))
                            vv7 = min(vv7, lim8)
                    cd["elevs"][k7] = max(cd["elevs"][k7], vv7)
    # ≥2 stations: a SINGLETON stub between two junctions/aprons is a real
    # corridor (two mouth anchors, one plane) — the old ≥3 floor silently
    # dropped every unchained stub, so nothing profiled or tied them and
    # their surroundings followed raw relief (s77p3, user at HECA #198:
    # "the taxi corridors passing through this apron are not being
    # graded" — taxiway B = five singleton stubs, all dropped here).
    if _os.environ.get("O4_CORR_CHDBG") == "1":
        for cd in chain_data:
            refs9 = sorted({rects[ri]["shape"].ref or "?"
                            for (ri, _n, _f) in cd["chain"]})
            print(f"[chdbg] L={cd['L']:.0f} sts={len(cd['stations'])} "
                  f"{refs9}"
                  + ("" if cd["L"] >= 30.0
                     and len(cd["stations"]) >= 2 else " DROPPED"))
    if NETWORK_PROFILE_MODEL:
        # every ≥2-station chain profiles from the field — a singleton
        # stub between two junctions/aprons is a real corridor (two mouth
        # samples, one plane); the ≥3/30 m floor existed to keep the TIE
        # layer's flat seeds honest (s77p3: dropping taxiway B's five
        # stubs here left its surroundings on raw relief)
        chain_data = [cd for cd in chain_data
                      if len(cd["stations"]) >= 2]
    else:
        chain_data = [cd for cd in chain_data
                      if cd["L"] >= 30.0 and len(cd["stations"]) >= 3]

    # ── STAGE B: JOINT CORRIDOR-NETWORK TIES.  Where chains meet they
    # must AGREE — the shared elevation is a COMMON variable:
    #   * two chains' junction-gap segments CROSS → one shared station
    #     inserted into BOTH chains (EQUALITY: one physical point, one
    #     elevation);
    #   * a chain TERMINUS abuts another chain's crossing run → a station
    #     at the projection + a grade-cap tie over the lateral offset
    #     (the T-junction continuation);
    #   * stations of different chains sharing canonical NODES → equality;
    #   * mouth stations of different chains at the SAME junction → a
    #     grade-cap tie over their chord, only when the chord stays
    #     inside the junction (a chord over a void is fictitious — the
    #     s73 junction-visibility lesson; it would pin arms flat again).

    def _seg_x(P0, P1, Q0, Q1):
        rX, rY = P1[0] - P0[0], P1[1] - P0[1]
        sX, sY = Q1[0] - Q0[0], Q1[1] - Q0[1]
        den = rX * sY - rY * sX
        if abs(den) < 1e-9:
            return None
        qpX, qpY = Q0[0] - P0[0], Q0[1] - P0[1]
        t = (qpX * sY - qpY * sX) / den
        u = (qpX * rY - qpY * rX) / den
        if 0.02 < t < 0.98 and 0.02 < u < 0.98:
            return t, u
        return None

    pend: list = []                   # pending station inserts [ci, d]
    eq_pairs: list = []               # (token, token) equality ties
    cap_ties: list = []               # (token, token, length_m) grade ties
    j_gaps: dict = {}
    for ci, cd in enumerate(chain_data):
        for gi, g in enumerate(cd["gaps"]):
            j_gaps.setdefault(g["ji"], []).append((ci, gi))
    for ji in sorted(j_gaps):
        lst = j_gaps[ji]
        for a in range(len(lst)):
            for b in range(a + 1, len(lst)):
                ca, ga = lst[a]
                cb, gb = lst[b]
                if ca == cb:
                    continue
                GA = chain_data[ca]["gaps"][ga]
                GB = chain_data[cb]["gaps"][gb]
                hit = _seg_x(GA["A"], GA["B"], GB["A"], GB["B"])
                if hit is None:
                    continue
                t, u = hit
                X = (GA["A"][0] + t * (GA["B"][0] - GA["A"][0]),
                     GA["A"][1] + t * (GA["B"][1] - GA["A"][1]))
                ta = ("p", len(pend))
                pend.append([ca, GA["dA"] + t * (GA["dB"] - GA["dA"]),
                             ji, X])
                tb = ("p", len(pend))
                pend.append([cb, GB["dA"] + u * (GB["dB"] - GB["dA"]),
                             ji, X])
                eq_pairs.append((ta, tb))
    for ci, cd in enumerate(chain_data):
        for k in (0, len(cd["stations"]) - 1):
            st = cd["stations"][k]
            if st["junc"] is None:
                continue
            for (cj, gj) in j_gaps.get(st["junc"], ()):
                if cj == ci:
                    continue
                G = chain_data[cj]["gaps"][gj]
                Ax, Ay = G["A"]
                Bx, By = G["B"]
                gl2 = (Bx - Ax) ** 2 + (By - Ay) ** 2
                if gl2 < 4.0:
                    continue
                t = (((st["mid"][0] - Ax) * (Bx - Ax)
                      + (st["mid"][1] - Ay) * (By - Ay)) / gl2)
                if not 0.02 < t < 0.98:
                    continue
                px = Ax + t * (Bx - Ax)
                py = Ay + t * (By - Ay)
                lat = math.hypot(st["mid"][0] - px, st["mid"][1] - py)
                if lat > G["band"]:
                    continue
                if not _junc_chord_ok(st["junc"], st["mid"], (px, py)):
                    continue
                tp = ("p", len(pend))
                pend.append([cj, G["dA"] + t * (G["dB"] - G["dA"]),
                             st["junc"], (px, py)])
                cap_ties.append((("s", ci, k), tp, max(lat, 1.0)))
    node_owner: dict = {}
    j_sts: dict = {}
    for ci, cd in enumerate(chain_data):
        for k, st in enumerate(cd["stations"]):
            if st["junc"] is not None:
                j_sts.setdefault(st["junc"], []).append((ci, k))
            for i in sorted(st["nodes"]):
                o = node_owner.get(i)
                if o is None:
                    node_owner[i] = (ci, k)
                elif o[0] != ci:
                    eq_pairs.append((("s", o[0], o[1]), ("s", ci, k)))
    for ji in sorted(j_sts):
        lst = j_sts[ji]
        for a in range(len(lst)):
            for b in range(a + 1, len(lst)):
                ca, ka = lst[a]
                cb, kb = lst[b]
                if ca == cb:
                    continue
                sta = chain_data[ca]["stations"][ka]
                stb = chain_data[cb]["stations"][kb]
                gap = _junc_geo_dist(ji, sta["mid"], stb["mid"])
                if WRITE_ARBITRATION and nodes is not None:
                    # the stations' values land on their NODE sets, so the
                    # binding distance is the NEAREST node pair over the
                    # in-junction surface, not mid-to-mid (HECA #256: mids
                    # 74-82 m apart where the mouth corners sit 11.5 m —
                    # the W2 skew lesson at the tie layer; a mid-mid cap
                    # of ~1.2 m let two chains hold a 2.7 m wall across
                    # one ring edge)
                    for ia2 in sta["nodes"]:
                        for ib2 in stb["nodes"]:
                            if ia2 >= len(nodes) or ib2 >= len(nodes):
                                continue
                            g2 = _junc_geo_dist(
                                ji, nodes[ia2], nodes[ib2])
                            if g2 is not None and (gap is None
                                                   or g2 < gap):
                                gap = g2
                if gap is None:
                    continue
                cap_ties.append((("s", ca, ka), ("s", cb, kb),
                                 max(gap, 2.0)))
    if _os.environ.get("O4_CORR_JDUMP"):
        tgt9 = {int(x) for x in
                _os.environ["O4_CORR_JDUMP"].split(",") if x.strip()}
        for ji, J9 in enumerate(juncs):
            if not (tgt9 & {i for i in J9["idxs"] if i is not None}):
                continue
            print(f"[jdump] ji={ji} ring-n={len(J9['idxs'])} "
                  f"stations={[(sorted({rects[ri]['shape'].ref or '?' for (ri, _n, _f) in chain_data[ci]['chain']}), round(chain_data[ci]['stations'][k]['d'])) for (ci, k) in j_sts.get(ji, ())]} "
                  f"gaps={[sorted({rects[ri]['shape'].ref or '?' for (ri, _n, _f) in chain_data[ci]['chain']}) for (ci, gi) in j_gaps.get(ji, ())]}")
            here9 = set(j_sts.get(ji, ()))
            for (ta9, tb9, ln9) in cap_ties:
                hit9 = ((ta9[0] == "s" and (ta9[1], ta9[2]) in here9)
                        or (tb9[0] == "s" and (tb9[1], tb9[2]) in here9))
                if hit9:
                    print(f"[jdump]   cap_tie {ta9} <-> {tb9} "
                          f"len={ln9:.1f}")
            for (ta9, tb9) in eq_pairs:
                hit9 = ((ta9[0] == "s" and (ta9[1], ta9[2]) in here9)
                        or (tb9[0] == "s" and (tb9[1], tb9[2]) in here9))
                if hit9:
                    print(f"[jdump]   eq {ta9} <-> {tb9}")
    # BRIDGE TIES: an UNCHAINED rect linking two different junctions is a
    # real grade path between them — without it a corridor's profile can
    # descend legally along its own route while the short bridge reads
    # the whole drop (SPJC R1: 23 m wide between two R-chain junctions,
    # 1.1 m = 4.8 %).  Tie every station pair across the bridge at the
    # through-path geodesic (in-junction legs + bridge span — euclidean
    # between mids would over-tighten around corners, the s73 lesson).
    chained_r: set = set()
    for c in chains:
        chained_r.update(ri for (ri, _n, _f) in c)
    for ri, r in enumerate(rects):
        if ri in chained_r:
            continue
        ja = r["mouths"][0].get("junc")
        jb = r["mouths"][1].get("junc")
        if (ja is None or jb is None or ja == jb
                or ja not in j_sts or jb not in j_sts):
            continue
        def _st_geo(jx, st, mouth_mid):
            # tightest in-junction leg: the station VALUE applies at every
            # member node, so the nearest node binds (mid-only legs left
            # SPJC R2's bridge cap ~1.3 m loose)
            ptsq = [st["mid"]] if st["mid"] is not None else []
            if nodes is not None:
                ptsq.extend(nodes[i4] for i4 in st["nodes"]
                            if i4 < len(nodes))
            best = None
            for p4 in ptsq:
                g4 = _junc_geo_dist(jx, p4, mouth_mid)
                if g4 is not None and (best is None or g4 < best):
                    best = g4
            return best

        for (ca, ka) in j_sts[ja]:
            ga2 = _st_geo(ja, chain_data[ca]["stations"][ka],
                          r["mouths"][0]["mid"])
            if ga2 is None:
                continue
            for (cb, kb) in j_sts[jb]:
                if ca == cb and ka == kb:
                    continue
                gb2 = _st_geo(jb, chain_data[cb]["stations"][kb],
                              r["mouths"][1]["mid"])
                if gb2 is None:
                    continue
                # Tie distance = the rect's SPAN, not the through-path
                # ga+span+gb: the bridge rect emits ONE plane over span,
                # and the twist paints each junction's surface from its
                # corridor value — the in-junction legs never actually
                # carry grade.  Through-path over-credited distance and
                # left tie-legal configurations whose emitted plane runs
                # the full junction-to-junction difference over 23-28 m
                # (SPJC R1/R2: parallel R/Q corridors 1.0-1.4 m apart,
                # rungs read 4.4-4.9 % with freeze-skipped=0).
                cap_ties.append((("s", ca, ka), ("s", cb, kb),
                                 max(r["span"], 2.0)))

    # SAME-APRON TERMINUS PAIRS: two chains whose termini open into the
    # SAME free apron are physically connected by that apron's surface,
    # but the apron is not a corridor member so no tie ever forms — the
    # two profiles freeze independently and the apron between them keeps
    # the disagreement (HECA #190: J's tail and G2's head 52 m apart at
    # 0.9 m).  Collect pairs ≤120 m here; they are reconciled by MINIMAL
    # PAIRWISE PROJECTION after the freeze (routing them through the
    # consensus wish system instead dragged each terminus toward its own
    # chain's INTERIOR wish — J's tail fell to ~66 against G2's 68.1 and
    # the seam grew to 1.9 m; measured, rejected).
    apron_end_pairs: list = []
    if apron_sets:
        term_by_apron: dict = {}
        for ci9, cd9 in enumerate(chain_data):
            sts9 = cd9["stations"]
            for k9 in (0, len(sts9) - 1):
                st9 = sts9[k9]
                if st9.get("junc") is not None or st9["mid"] is None:
                    continue
                for ai9, aset9 in enumerate(apron_sets):
                    if st9["nodes"] & aset9:
                        term_by_apron.setdefault(ai9, []).append(
                            (ci9, k9))
        for ai9 in sorted(term_by_apron):
            lst9 = term_by_apron[ai9]
            for a9 in range(len(lst9)):
                for b9 in range(a9 + 1, len(lst9)):
                    ca9, ka9 = lst9[a9]
                    cb9, kb9 = lst9[b9]
                    if ca9 == cb9:
                        continue
                    ma9 = chain_data[ca9]["stations"][ka9]["mid"]
                    mb9 = chain_data[cb9]["stations"][kb9]["mid"]
                    d9 = math.hypot(ma9[0] - mb9[0], ma9[1] - mb9[1])
                    if d9 > 120.0:
                        continue
                    apron_end_pairs.append(
                        (ca9, ka9, cb9, kb9, max(d9, 2.0)))

    # apply pending inserts; a pending point within 3 m of an existing or
    # already-inserted station BINDS to it instead (sub-5 m stations
    # spiral ``faa_rate_of_change_pass`` — the s73 runway lesson)
    idx_map: dict = {}
    by_chain: dict = {}
    for pi, p in enumerate(pend):
        by_chain.setdefault(p[0], []).append(pi)
    for ci in sorted(by_chain):
        cd = chain_data[ci]
        sts = cd["stations"]
        new_ds: list = []                 # (d, junc, mid)
        for pi in sorted(by_chain[ci], key=lambda q: pend[q][1]):
            dq = pend[pi][1]
            if (any(abs(s["d"] - dq) <= 3.0 for s in sts)
                    or any(abs(x[0] - dq) <= 3.0 for x in new_ds)):
                continue
            new_ds.append((dq, pend[pi][2], pend[pi][3]))
        if not new_ds:
            continue
        old_ds = [s["d"] for s in sts]
        old_es = list(cd["elevs"])
        merged = sorted([(s["d"], 0, oi) for oi, s in enumerate(sts)]
                        + [(dq, 1, oi) for oi, dq in
                           enumerate(x[0] for x in new_ds)])
        amap: dict = {}
        n_sts: list = []
        n_es: list = []
        n_hd: list = []
        n_an: list = []
        for dq, kind, oi in merged:
            if kind == 0:
                amap[oi] = len(n_sts)
                n_sts.append(sts[oi])
                n_es.append(cd["elevs"][oi])
                n_hd.append(cd["hard"][oi])
                n_an.append(cd["anchored"][oi])
            else:
                n_sts.append({"d": dq, "nodes": set(),
                              "mid": new_ds[oi][2], "halfw": 0.0,
                              "junc": new_ds[oi][1]})
                n_es.append(_interp_profile(old_ds, old_es, dq))
                n_hd.append(False)
                n_an.append(False)
        cd["stations"] = n_sts
        cd["elevs"] = n_es
        cd["hard"] = n_hd
        cd["anchored"] = n_an
        cd["mouth_st"] = [[amap[a], amap[b]] for (a, b) in cd["mouth_st"]]
        idx_map[ci] = amap

    # junction HARD bands (runway contacts / immutable seeds on junction
    # rings): used by the TWIST pass in both models, and by the tie
    # layer's station bands gate-off.  (Hoisted — nothing between here
    # and the old build site mutates elev/juncs/base_hard.)
    jhard: dict = {}
    for ji, J in enumerate(juncs):
        hp = [(J["ring"][k2], elev[i2], i2)
              for k2, i2 in enumerate(J["idxs"])
              if i2 is not None and (base_hard[i2] or i2 in rwy_nodes)]
        if hp:
            jhard[ji] = hp

    if NETWORK_PROFILE_MODEL:
        # ── NETWORK PROFILE MODEL (#4): one field on the full centerline
        # graph replaces the tie layer below — stations SAMPLE the field,
        # shared physical points agree by construction (M2), and the
        # runway-flex demands are measured directly on the field (M3).
        dem_lo, dem_hi, dem_refs = _network_field_stations(
            layout, elev, bucket_to_idx, chain_data, rwy_nodes,
            rwy_ref_of, nodes, exit_overrides, dem_ctx,
            base_hard=base_hard)
    else:
        def _resolve(tok):
            if tok[0] == "s":
                _t, ci, k = tok
                return (ci, idx_map[ci][k]) if ci in idx_map else (ci, k)
            ci, dq = pend[tok[1]][0], pend[tok[1]][1]
            sts = chain_data[ci]["stations"]
            k = min(range(len(sts)), key=lambda q: abs(sts[q]["d"] - dq))
            return (ci, k)

        parent: dict = {}

        def _find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for ta, tb in eq_pairs:
            a, b = _resolve(ta), _resolve(tb)
            if a != b:
                parent[_find(a)] = _find(b)
        tie_keys: set = set(parent)
        edges_r: list = []
        for ta, tb, ln in cap_ties:
            a, b = _resolve(ta), _resolve(tb)
            if a == b or _find(a) == _find(b):
                continue
            edges_r.append((a, b, ln))
            tie_keys.add(a)
            tie_keys.add(b)
        gid_of: dict = {}
        g_members: list = []
        for key in sorted(tie_keys):
            rt = _find(key)
            if rt not in gid_of:
                gid_of[rt] = len(g_members)
                g_members.append([])
            g_members[gid_of[rt]].append(key)
        root_of = {key: gid_of[_find(key)] for key in tie_keys}
        g_edges: dict = {}
        for a, b, ln in edges_r:
            kk = (min(root_of[a], root_of[b]), max(root_of[a], root_of[b]))
            g_edges[kk] = min(g_edges.get(kk, float("inf")), ln)

        # a tied TERMINUS is no longer pinned at its stale network value —
        # the tie IS its connection (the stale pin was the disagreement)
        for (ci, k) in sorted(tie_keys):
            if not chain_data[ci]["hard"][k]:
                chain_data[ci]["anchored"][k] = False
        # …but a tie COMPONENT with no anchor anywhere re-anchors its termini
        # (nothing else pins it to the network)
        cpar = list(range(len(chain_data)))

        def _cf(x):
            while cpar[x] != x:
                cpar[x] = cpar[cpar[x]]
                x = cpar[x]
            return x

        for members in g_members:
            for (ci, _k) in members[1:]:
                cpar[_cf(members[0][0])] = _cf(ci)
        for (ga, gb) in g_edges:
            cpar[_cf(g_members[ga][0][0])] = _cf(g_members[gb][0][0])
        comp_anch: dict = {}
        for ci, cd in enumerate(chain_data):
            rt = _cf(ci)
            comp_anch[rt] = comp_anch.get(rt, False) or any(cd["anchored"])
        for ci, cd in enumerate(chain_data):
            if not comp_anch.get(_cf(ci), True):
                cd["anchored"][0] = True
                cd["anchored"][-1] = True

        # ANCHOR SELF-CONSISTENCY: a chain whose anchors are mutually
        # cap-infeasible (HECA T4+U: hard runway contact 110.4 at one end,
        # DEM-settled terminus 98.4 at the other, 611 m = 1.96 %) can satisfy
        # NOTHING in between — every consensus tie freeze-fails and the
        # profile rides an over-cap ramp.  Project the NON-HARD anchors onto
        # pairwise cap feasibility (hard ones immovable: correct grade is
        # king, the network terminus value is only the DEM starting point).
        for cd in chain_data:
            sts = cd["stations"]
            # an extended SINGLETON exit stub keeps its far terminus at the
            # surrounding network value: relaxing it toward the runway
            # virtual crushed the stub flat and left the whole gap as a
            # cliff at the apron seam (HECA A5 ↔ #284 read 3.9 m) — the
            # infeasible remainder stays as honest stub steepness instead
            if (len(cd["chain"]) == 1
                    and any(s5.get("virtual") for s5 in sts)):
                continue
            anc = [k for k in range(len(sts)) if cd["anchored"][k]]
            for _sweep in range(20):
                worst = 0.0
                for a2 in range(len(anc)):
                    for b2 in range(a2 + 1, len(anc)):
                        j, k = anc[a2], anc[b2]
                        dd = abs(sts[k]["d"] - sts[j]["d"])
                        lim = TAXI_MAX_GRADE * dd + 0.01
                        diff = cd["elevs"][k] - cd["elevs"][j]
                        ex = abs(diff) - lim
                        if ex <= 0.0:
                            continue
                        hj, hk = cd["hard"][j], cd["hard"][k]
                        if hj and hk:
                            continue          # runway-flex territory
                        sgn = 1.0 if diff > 0 else -1.0
                        if hj:
                            cd["elevs"][k] -= sgn * ex
                        elif hk:
                            cd["elevs"][j] += sgn * ex
                        else:
                            cd["elevs"][k] -= sgn * ex / 2.0
                            cd["elevs"][j] += sgn * ex / 2.0
                        worst = max(worst, ex)
                if worst < 0.01:
                    break

        # per-station bands = runway-route bands (post-insert) ∩ junction
        # HARD bands.  A corridor station at / inside a junction is capped by
        # the junction's HARD ring vertices (runway contacts, immutable seeds)
        # over the in-junction chord — the route bands cannot see a runway one
        # junction-width away when the centerline graph under-connects there
        # (CYXY #74: chain E's flat seed lifted its crossing 2.9 m above a
        # runway vertex 20 m across the junction it crosses).
        for cd in chain_data:
            st_lo, st_hi = [], []
            # the route-reach graph CUTS curve corners (same data flaw as the
            # exit centerlines), so its bands under-measure and cap the very
            # climb the virtual runway anchor grants (A5's mouth: chain says
            # ~63.6, graph ceiling ~61 → clamped flat).  At stations of a
            # virtual-anchored chain the chain's own curve-aware distance
            # supersedes: only RELAXES the band, never tightens.
            virts6 = [(st6["d"], cd["elevs"][k6])
                      for k6, st6 in enumerate(cd["stations"])
                      if st6.get("virtual")]
            for st in cd["stations"]:
                ns = st["nodes"]
                slo, shi = float("-inf"), float("inf")
                if reach_lo is not None:
                    slo = max((reach_lo[i] for i in ns if i < len(reach_lo)),
                              default=float("-inf"))
                    shi = min((reach_hi[i] for i in ns if i < len(reach_hi)),
                              default=float("inf"))
                    if slo > shi:                  # infeasible: leave free
                        slo, shi = float("-inf"), float("inf")
                ji = st.get("junc")
                if ji in jhard:
                    # the station value lands on EVERY member node — the cap
                    # must hold at the tightest node position, not just the
                    # mouth midpoint (the corner nearer the runway binds)
                    pts = []
                    if st["mid"] is not None:
                        pts.append(st["mid"])
                    if nodes is not None:
                        pts.extend(nodes[i2] for i2 in ns if i2 < len(nodes))
                    hlo, hhi = float("-inf"), float("inf")
                    for (hp, he, _hi2) in jhard[ji]:
                        for pt in pts:
                            dd = _junc_geo_dist(ji, pt, hp)
                            if dd is None:
                                continue
                            hlo = max(hlo, he - TAXI_MAX_GRADE * dd - 0.02)
                            hhi = min(hhi, he + TAXI_MAX_GRADE * dd + 0.02)
                    if hlo <= hhi:
                        if max(slo, hlo) <= min(shi, hhi):
                            slo, shi = max(slo, hlo), min(shi, hhi)
                        else:
                            slo, shi = hlo, hhi    # local hard cap wins
                if virts6:
                    hi6 = min(vv6 + TAXI_MAX_GRADE * abs(st["d"] - vd6)
                              for (vd6, vv6) in virts6)
                    lo6 = max(vv6 - TAXI_MAX_GRADE * abs(st["d"] - vd6)
                              for (vd6, vv6) in virts6)
                    shi = max(shi, hi6)
                    slo = min(slo, lo6)
                st_lo.append(slo)
                st_hi.append(shi)
            cd["st_lo"], cd["st_hi"] = st_lo, st_hi

        # ── JOINT SOLVE: one value per tie group, found by consensus
        # iteration.  Each chain's WISH for a tie station is the flat
        # interpolation between its flanking pins (anchored stations + its
        # OTHER tie stations at their current group values) — the flat-seed
        # preference expressed pointwise.  Groups average member wishes
        # (anchored members are immovable), clamp into the intersected route
        # band, and the grade-cap ties project pairs together.  Caps/Δg are
        # enforced by the final per-chain solve below; the FREEZE rejects any
        # consensus value a chain genuinely cannot cap-reach.
        def _chain_feas(ci, k):
            """Cap-feasibility interval for station k of chain ci against the
            chain's (static) anchors — a consensus value outside it would only
            be rejected at freeze time, leaving the cliff in place.  The
            station's OWN anchor never bounds (an anchored terminus member
            would otherwise pin its whole group's band to its DEM value)."""
            cd = chain_data[ci]
            sts = cd["stations"]
            lo, hi = float("-inf"), float("inf")
            for j in range(len(sts)):
                if j == k and WRITE_ARBITRATION:
                    continue
                if not cd["anchored"][j]:
                    continue
                dd = abs(sts[k]["d"] - sts[j]["d"])
                lo = max(lo, cd["elevs"][j] - TAXI_MAX_GRADE * dd)
                hi = min(hi, cd["elevs"][j] + TAXI_MAX_GRADE * dd)
            return lo, hi

        def _sv_chain(ci):
            """Extended SINGLETON exit stub (virtual runway anchor): its far
            terminus keeps the surrounding network value — the established
            anchor-self-consistency exemption (relaxing it toward the network
            crushed A5 flat in p10; the s73-p10c ruling pins A5 ≈ 60.4).  The
            write-arbitration move paths honour the same exemption."""
            cd9 = chain_data[ci]
            return (len(cd9["chain"]) == 1
                    and any(s5.get("virtual") for s5 in cd9["stations"]))

        g_fix: list = []
        g_soft: list = []                  # anchored-but-SOFT members per group
        g_val: list = []
        g_lo: list = []
        g_hi: list = []
        for members in g_members:
            # A NON-HARD TERMINUS anchor is a DEM-settled STARTING point, not
            # a route demand (the p10d apron-mouth lesson, generalized to
            # junction mouths — s77 write arbitration): it must not FIX its
            # tie group, or the corridor network can never lift a chain end
            # toward the values its junction partners carry (HECA #256: G's
            # mouth anchored ~100.9 fixed the group while T's route law sat
            # at 102.3-103.7 across the same junction = a held 2.7-3.3 m
            # wall).  Such members join the consensus as a DEM wish and are
            # PROJECTED to the settled group value afterwards (bounded).
            hardf: list = []
            softf: list = []
            for (ci, k) in members:
                cd9 = chain_data[ci]
                if not cd9["anchored"][k]:
                    continue
                sts9 = cd9["stations"]
                if (WRITE_ARBITRATION
                        and k in (0, len(sts9) - 1)
                        and not cd9["hard"][k]
                        and not sts9[k].get("virtual")
                        and not _sv_chain(ci)
                        and not (sts9[k]["nodes"] & rwy_nodes)):
                    softf.append((ci, k))
                else:
                    hardf.append((ci, k))
            g_fix.append(bool(hardf))
            g_soft.append(softf)
            fixv = [chain_data[ci]["elevs"][k] for (ci, k) in hardf + softf]
            vals = fixv or [chain_data[ci]["elevs"][k] for (ci, k) in members]
            g_val.append(sum(vals) / len(vals))
            lo, hi = float("-inf"), float("inf")
            for (ci, k) in members:
                cd = chain_data[ci]
                if cd["st_lo"] is not None:
                    lo = max(lo, cd["st_lo"][k])
                    hi = min(hi, cd["st_hi"][k])
            if lo > hi:
                lo, hi = float("-inf"), float("inf")
            # intersect with every member chain's anchor feasibility — when
            # jointly feasible the consensus stays freezable by construction;
            # when not, keep the band (the freeze guard then keeps the
            # reachable members and leaves the rest as honest conflicts)
            flo, fhi = lo, hi
            for (ci, k) in members:
                a, b = _chain_feas(ci, k)
                flo, fhi = max(flo, a), min(fhi, b)
            if flo <= fhi:
                lo, hi = flo, fhi
            g_lo.append(lo)
            g_hi.append(hi)

        def _wish(ci, k):
            cd = chain_data[ci]
            sts = cd["stations"]

            def _pv(j):
                if cd["anchored"][j]:
                    return cd["elevs"][j]
                return g_val[root_of[(ci, j)]]

            lk = rk = None
            for j in range(k - 1, -1, -1):
                if cd["anchored"][j] or (ci, j) in root_of:
                    lk = j
                    break
            for j in range(k + 1, len(sts)):
                if cd["anchored"][j] or (ci, j) in root_of:
                    rk = j
                    break
            if lk is not None and rk is not None:
                da, db = sts[lk]["d"], sts[rk]["d"]
                if db - da < 1e-9:
                    return _pv(lk)
                t = (sts[k]["d"] - da) / (db - da)
                return _pv(lk) + t * (_pv(rk) - _pv(lk))
            if lk is not None:
                return _pv(lk)
            if rk is not None:
                return _pv(rk)
            return cd["elevs"][k]

        n_rounds = 0
        for _rnd in range(60):
            n_rounds = _rnd + 1
            new_val = []
            for gid, members in enumerate(g_members):
                if g_fix[gid]:
                    new_val.append(g_val[gid])
                    continue
                ws = [_wish(ci, k) for (ci, k) in members]
                # soft-anchored termini keep a DEM pull in the average (their
                # settled value is a preference even though it no longer fixes)
                ws += [chain_data[ci]["elevs"][k] for (ci, k) in g_soft[gid]]
                v = sum(ws) / len(ws)
                # damped (oscillation between wish-average and the cap-tie
                # projection never settled at 30 undamped rounds)
                v = 0.5 * g_val[gid] + 0.5 * v
                new_val.append(min(max(v, g_lo[gid]), g_hi[gid]))
            for (ga, gb) in sorted(g_edges):
                lim = TAXI_MAX_GRADE * g_edges[(ga, gb)] + 0.02
                diff = new_val[ga] - new_val[gb]
                ex = abs(diff) - lim
                if ex <= 0.0 or (g_fix[ga] and g_fix[gb]):
                    continue
                sgn = 1.0 if diff > 0 else -1.0
                if g_fix[ga]:
                    new_val[gb] += sgn * ex
                elif g_fix[gb]:
                    new_val[ga] -= sgn * ex
                else:
                    new_val[ga] -= sgn * ex / 2.0
                    new_val[gb] += sgn * ex / 2.0
            for gid in range(len(new_val)):
                if not g_fix[gid] and g_lo[gid] <= g_hi[gid]:
                    new_val[gid] = min(max(new_val[gid], g_lo[gid]),
                                       g_hi[gid])
            moved = max((abs(va - vb) for va, vb in zip(new_val, g_val)),
                        default=0.0)
            g_val = new_val
            if moved < 0.005:
                break

        # CAP-TIE RESIDUAL ARBITRATION (s77, write-layer arbitration): the
        # damped rounds clamp every group back into its ROUTE-BAND each pass,
        # and two groups a junction apart can carry st-bands METRES apart
        # (cross-chain route-graph entry noise — at HECA #256 the st-band
        # ceiling said ~100.9 where the per-vertex band FLOOR said 104.17),
        # so the loop can end with its cap ties still violated and nothing
        # downstream arbitrates the residual: the two chains write a wall.
        # Resolve the residual WITHOUT the band clamp — at this scale the
        # bands disagree with each other by more than the residual, so the
        # corridor network's own cap compatibility is the better truth.  The
        # freeze below still verifies every value against each member
        # chain's HARD anchors (and partial-clamps into them), so a real
        # route demand is never overridden.
        if WRITE_ARBITRATION and g_edges:
            n_arb = 0
            w_arb = 0.0
            for _rnd2 in range(40):
                worst2 = 0.0
                for (ga, gb) in sorted(g_edges):
                    lim = TAXI_MAX_GRADE * g_edges[(ga, gb)] + 0.02
                    diff = g_val[ga] - g_val[gb]
                    ex = abs(diff) - lim
                    if ex <= 0.005 or (g_fix[ga] and g_fix[gb]):
                        continue
                    sgn = 1.0 if diff > 0 else -1.0
                    if g_fix[ga]:
                        g_val[gb] += sgn * ex
                    elif g_fix[gb]:
                        g_val[ga] -= sgn * ex
                    else:
                        g_val[ga] -= sgn * ex / 2.0
                        g_val[gb] += sgn * ex / 2.0
                    n_arb += 1
                    w_arb = max(w_arb, ex)
                    worst2 = max(worst2, ex)
                if worst2 < 0.01:
                    break
            if _os.environ.get("O4_CORRIDOR_DEBUG") == "1" and n_arb:
                print(f"[corr] cap-tie arbitration: {n_arb} projection(s), "
                      f"worst residual start {w_arb:.2f}m")

        # SOFT-TERMINUS PROJECTION (s77 write arbitration): re-anchor each
        # soft terminus at its group's settled value, bounded by (1) the
        # chain's OTHER anchors at the taxi cap, (2) the junction's HARD band
        # at the mouth, and (3) a max-move guard (the p10d J-tail lesson: an
        # UNBOUNDED move let a 0.33 m infeasibility fall 4.7 m flat = a
        # manufactured 5.3 m wall — projection, never free-fall).
        if WRITE_ARBITRATION:
            for gid, softs in enumerate(g_soft):
                for (ci, k) in softs:
                    cd = chain_data[ci]
                    sts = cd["stations"]
                    cur = cd["elevs"][k]
                    v = g_val[gid]
                    if abs(v - cur) <= 0.005:
                        continue
                    lo9 = cur - _TERM_PROJ_MAX_M
                    hi9 = cur + _TERM_PROJ_MAX_M
                    for j in range(len(sts)):
                        if j == k or not cd["anchored"][j]:
                            continue
                        lim9 = (TAXI_MAX_GRADE
                                * abs(sts[k]["d"] - sts[j]["d"]) + 0.02)
                        lo9 = max(lo9, cd["elevs"][j] - lim9)
                        hi9 = min(hi9, cd["elevs"][j] + lim9)
                    ji9 = sts[k].get("junc")
                    if ji9 in jhard and sts[k]["mid"] is not None:
                        for (hp9, he9, _h9) in jhard[ji9]:
                            dd9 = _junc_geo_dist(ji9, sts[k]["mid"], hp9)
                            if dd9 is None:
                                continue
                            lo9 = max(lo9, he9 - TAXI_MAX_GRADE * dd9 - 0.02)
                            hi9 = min(hi9, he9 + TAXI_MAX_GRADE * dd9 + 0.02)
                    if lo9 > hi9:
                        continue
                    v2 = min(max(v, lo9), hi9)
                    if abs(v2 - cur) <= 0.005:
                        continue
                    if _os.environ.get("O4_CORRIDOR_DEBUG") == "1":
                        refs9 = sorted({rects[ri]["shape"].ref or "?"
                                        for (ri, _n, _f) in cd["chain"]})
                        print(f"[corr]   term-arb chain={refs9} "
                              f"d={sts[k]['d']:.0f} {cur:.2f} -> {v2:.2f} "
                              f"(group v={v:.2f})")
                    cd["elevs"][k] = v2

        # FREEZE the consensus into each chain: tie stations become anchors —
        # most-constrained (largest) groups first; a value a chain cannot
        # cap-reach from its existing anchors is SKIPPED (left free: an
        # honest local conflict beats a manufactured cliff).  When the
        # blocking anchor is a RUNWAY CONTACT, the skip is converted into a
        # RUNWAY FLEX DEMAND (user model: pavement grades to max FIRST, then
        # the runway flexes the minimum — HECA T4+U needs the 05C contact at
        # ~107.5, not 110.4, for the corridor to fit at 1.5 %): the caller
        # re-smooths the runway through these bounds and re-runs the pass.
        n_skip = 0
        dem_lo: dict = {}
        dem_hi: dict = {}
        dem_refs: set = set()
        for ci, cd in enumerate(chain_data):
            sts = cd["stations"]
            ks = [k for k in range(len(sts))
                  if (ci, k) in root_of and not cd["anchored"][k]]
            for k in sorted(ks, key=lambda q:
                            (-len(g_members[root_of[(ci, q)]]), q)):
                v = g_val[root_of[(ci, k)]]
                ok = all(abs(v - cd["elevs"][j])
                         <= TAXI_MAX_GRADE * abs(sts[k]["d"] - sts[j]["d"])
                         + 0.05
                         for j in range(len(sts)) if cd["anchored"][j])
                if not ok:
                    # BLOCKER RESCUE — classify each blocking anchor and move
                    # it MINIMALLY into the tie's reach; the tie is accepted
                    # only when EVERY blocker reaches (no side-effect moves):
                    #  (a) APRON-MOUTH UNTIED TERMINUS (junc=None, nodes
                    #      shared with a free apron): its anchor is the apron
                    #      edge's DEM-settled value — a STARTING point, not a
                    #      demand (HECA #25: apron anchor 64.67 froze out the
                    #      route-supported 61.76).  ★ Projection, NOT
                    #      un-anchoring — dropping the anchor let J's tail
                    #      (0.33 m infeasible) fall 4.7 m via the flat
                    #      extension = a manufactured 5.3 m wall.
                    #  (b) FROZEN-TIE MEMBER: an earlier-frozen consensus
                    #      value (non-hard) — the network was jointly
                    #      infeasible and the freeze order dumped the whole
                    #      disagreement here (HECA #261: G's closing tie
                    #      101.42 died against G's mid-chain frozen tie at
                    #      91.34 where 0.5 m spread over 636 m closes it).
                    #      The move is vetoed by every member chain's HARD
                    #      anchors and the group's cap ties; a clamp that
                    #      pushes the target PAST the tie (stale group band:
                    #      T d=302 went 104.82→106.45 chasing 102.00) means
                    #      "don't move", never a side-effect move.
                    #  Anything else (hard, runway contact, virtual) keeps
                    #  its veto — the freeze-skip stays an honest conflict.
                    blockers = [
                        j for j in range(len(sts))
                        if cd["anchored"][j]
                        and abs(v - cd["elevs"][j])
                        > TAXI_MAX_GRADE * abs(sts[k]["d"] - sts[j]["d"])
                        + 0.05]
                    plan9: list = []
                    feasible9 = bool(blockers)
                    for j in blockers:
                        if cd["hard"][j] or sts[j].get("virtual"):
                            feasible9 = False
                            break
                        dd9 = abs(sts[k]["d"] - sts[j]["d"])
                        lim9 = TAXI_MAX_GRADE * dd9 + 0.05
                        cur9 = cd["elevs"][j]
                        # project 1 cm INSIDE the limit — landing exactly on
                        # the boundary fails the float re-check (G's tie
                        # re-skipped at over-by 0.05 after a boundary move)
                        tgt = min(max(cur9, v - lim9 + 0.01),
                                  v + lim9 - 0.01)
                        if (ci, j) in root_of:
                            gb9 = root_of[(ci, j)]
                            for (cj2, kj2) in g_members[gb9]:
                                cd2 = chain_data[cj2]
                                sts2 = cd2["stations"]
                                for j2 in range(len(sts2)):
                                    if not cd2["hard"][j2]:
                                        continue
                                    dd2 = abs(sts2[kj2]["d"]
                                              - sts2[j2]["d"])
                                    lim2 = TAXI_MAX_GRADE * dd2 + 0.02
                                    tgt = min(
                                        max(tgt, cd2["elevs"][j2] - lim2),
                                        cd2["elevs"][j2] + lim2)
                            for (ga8, gb8), ln8 in g_edges.items():
                                other8 = (gb8 if ga8 == gb9
                                          else (ga8 if gb8 == gb9 else None))
                                if other8 is None:
                                    continue
                                lim3 = TAXI_MAX_GRADE * ln8 + 0.05
                                tgt = min(max(tgt, g_val[other8] - lim3),
                                          g_val[other8] + lim3)
                            if abs(tgt - v) > lim9:
                                feasible9 = False
                                break
                            plan9.append(("group", j, gb9, cur9, tgt))
                        elif (j in (0, len(sts) - 1)
                              and sts[j].get("junc") is None
                              and not (sts[j]["nodes"] & rwy_nodes)
                              and (sts[j]["nodes"] & apron_nodes)):
                            plan9.append(("term", j, None, cur9, tgt))
                        else:
                            feasible9 = False
                            break
                    if feasible9:
                        dbg8 = _os.environ.get("O4_CORRIDOR_DEBUG") == "1"
                        for (kind9, j, gb9, cur9, tgt) in plan9:
                            if abs(tgt - cur9) <= 0.005:
                                continue
                            if dbg8:
                                refs8 = sorted({rects[ri]["shape"].ref or "?"
                                                for (ri, _n, _f)
                                                in cd["chain"]})
                                print(f"[corr]   rescue {kind9} blocker "
                                      f"chain={refs8} d={sts[j]['d']:.0f} "
                                      f"{cur9:.2f} -> {tgt:.2f} "
                                      f"(tie v={v:.2f}@d={sts[k]['d']:.0f})")
                            if kind9 == "group":
                                g_val[gb9] = tgt
                                for (cj2, kj2) in g_members[gb9]:
                                    cd9 = chain_data[cj2]
                                    if not cd9["hard"][kj2]:
                                        cd9["elevs"][kj2] = tgt
                            else:
                                cd["elevs"][j] = tgt
                        ok = all(
                            abs(v - cd["elevs"][j])
                            <= TAXI_MAX_GRADE
                            * abs(sts[k]["d"] - sts[j]["d"]) + 0.05
                            for j in range(len(sts))
                            if cd["anchored"][j])
                if ok:
                    cd["elevs"][k] = v
                    cd["anchored"][k] = True
                else:
                    # PARTIAL TIE (write-layer arbitration, s77 user-approved):
                    # the rescue could not move the blockers, but dropping the
                    # tie entirely leaves this chain to be re-threaded by its
                    # OWN route bands — metres from the consensus, and the gap
                    # stands in the surface as a wall at the shared junction
                    # (HECA #256: tie wanted T@102.11, route anchor 105.65
                    # blocked, the dropped station re-threaded to ~104.2
                    # against G's held 100.9 = 3.3 m over 11.5 m).  Clamp the
                    # consensus into THIS member's anchor-feasible interval
                    # and anchor there: the chain moves as close to agreement
                    # as its own route law allows, and the wall shrinks to the
                    # genuine route-law residual.  The flex-demand synthesis
                    # below still measures the ORIGINAL consensus value, so a
                    # legitimate runway-flex demand is never masked.
                    partial = None
                    if WRITE_ARBITRATION and not _sv_chain(ci):
                        v_lo, v_hi = float("-inf"), float("inf")
                        for j in range(len(sts)):
                            if not cd["anchored"][j]:
                                continue
                            lim8 = (TAXI_MAX_GRADE
                                    * abs(sts[k]["d"] - sts[j]["d"]) + 0.05)
                            v_lo = max(v_lo, cd["elevs"][j] - lim8)
                            v_hi = min(v_hi, cd["elevs"][j] + lim8)
                        if v_lo <= v_hi and v_lo > float("-inf"):
                            # 1 cm inside the boundary (the float re-check
                            # lesson); degenerate-width intervals take the mid
                            if v_hi - v_lo > 0.02:
                                partial = min(max(v, v_lo + 0.01), v_hi - 0.01)
                            else:
                                partial = 0.5 * (v_lo + v_hi)
                    if partial is None:
                        n_skip += 1
                    for j in range(len(sts)):
                        if not cd["anchored"][j]:
                            continue
                        dj = abs(sts[k]["d"] - sts[j]["d"])
                        if abs(cd["elevs"][j] - v) <= TAXI_MAX_GRADE * dj \
                                + 0.05:
                            continue             # this anchor isn't blocking
                        # the blocked tie demands flex at every RUNWAY vertex
                        # this anchor stands on — directly (contact station)
                        # or THROUGH its junction (a terminus on the
                        # runway-adjacent junction: budget grows by the
                        # in-junction geodesic to the runway vertex)
                        targets = [(i, dj) for i in sts[j]["nodes"]
                                   if i in rwy_nodes]
                        # TRANSITIVE PROVENANCE (s77p2 user: "if the taxiway
                        # requires it, why isn't the runway already dipping
                        # enough? we shouldn't be generating a violation"):
                        # a blocker that is a frozen TIE station (crossing
                        # insert, no nodes) carries another chain's runway
                        # demand one tie hop away — HECA #256: T@d302's
                        # ~105 is pinned by the 05C contact THROUGH the
                        # crossing chain, so the dip the T↔G tie needs never
                        # reached the runway.  Walk the blocking tie group's
                        # member chains to their runway-contact anchors and
                        # demand over the ACCUMULATED route distance (the
                        # tie is one physical point — its legs add).
                        if (WRITE_ARBITRATION and not targets
                                and (ci, j) in root_of):
                            for (cj3, kj3) in g_members[root_of[(ci, j)]]:
                                cd3 = chain_data[cj3]
                                sts3 = cd3["stations"]
                                for j3 in range(len(sts3)):
                                    if not cd3["anchored"][j3]:
                                        continue
                                    rn3 = [i3 for i3 in sts3[j3]["nodes"]
                                           if i3 in rwy_nodes]
                                    if not rn3:
                                        continue
                                    d3 = dj + abs(sts3[kj3]["d"]
                                                  - sts3[j3]["d"])
                                    targets.extend((i3, d3) for i3 in rn3)
                        ji2 = sts[j].get("junc")
                        if (not targets and ji2 is not None
                                and ji2 in jhard
                                and sts[j]["mid"] is not None):
                            virt7 = bool(sts[j].get("virtual"))
                            for (hp, _he, hi2) in jhard[ji2]:
                                if hi2 not in rwy_nodes:
                                    continue
                                if virt7:
                                    # the virtual sits ON the runway: the dip
                                    # centres at the contact, so nearby
                                    # vertices carry the CONTACT need (adding
                                    # the contact→vertex leg diluted the T4
                                    # demand 107.9 → 109.6)
                                    dd7 = math.hypot(
                                        sts[j]["mid"][0] - hp[0],
                                        sts[j]["mid"][1] - hp[1])
                                    if dd7 <= 60.0:
                                        targets.append((hi2, dj))
                                    continue
                                gd = _junc_geo_dist(ji2, sts[j]["mid"], hp)
                                if gd is not None:
                                    targets.append((hi2, dj + gd))
                        # DIP demands only: a corridor squeezed against a HIGH
                        # runway is the saturated-pavement case the runway
                        # must absorb.  RISE demands trace to DEM-settled free
                        # pavement at the far end — per the priority model
                        # that pavement fills toward the runway instead (the
                        # J-chain class re-manufactured the 05L +1.1 rise the
                        # s73-p3 deadband killed; user-verified 05L stays
                        # 57.9-60.7).
                        for i, dtot in targets:
                            lim = TAXI_MAX_GRADE * dtot
                            if elev[i] - v > lim and elev[i] - (v + lim) \
                                    >= 0.5:      # vertex must DIP
                                dem_hi[i] = min(
                                    dem_hi.get(i, float("inf")), v + lim)
                                dem_refs.add(rwy_ref_of.get(i, ""))
                    if _os.environ.get("O4_CORRIDOR_DEBUG") == "1":
                        refs = sorted({rects[ri]["shape"].ref or "?"
                                       for (ri, _n, _f) in cd["chain"]})
                        wj, worst = -1, 0.0
                        for j in range(len(sts)):
                            if not cd["anchored"][j]:
                                continue
                            ex2 = (abs(v - cd["elevs"][j])
                                   - TAXI_MAX_GRADE
                                   * abs(sts[k]["d"] - sts[j]["d"]))
                            if ex2 > worst:
                                wj, worst = j, ex2
                        blk = ""
                        if wj >= 0:
                            blk = (f" blocker[d={sts[wj]['d']:.0f} "
                                   f"e={cd['elevs'][wj]:.2f} "
                                   f"hard={cd['hard'][wj]} "
                                   f"rwy={bool(sts[wj]['nodes'] & rwy_nodes)}"
                                   f" n={len(sts[wj]['nodes'])}]")
                        print(f"[corr]   freeze-"
                              + (f"partial chain={refs} d={sts[k]['d']:.0f} "
                                 f"v={v:.2f} -> {partial:.2f}"
                                 if partial is not None else
                                 f"skip chain={refs} d={sts[k]['d']:.0f} "
                                 f"v={v:.2f}")
                              + f" over-by={worst:.2f}m{blk}")
                    if partial is not None:
                        cd["elevs"][k] = partial
                        cd["anchored"][k] = True
            if not any(cd["anchored"]):
                cd["anchored"][0] = True
                cd["anchored"][-1] = True

        # POST-FREEZE TIE RECONCILE (s77 write arbitration): the freeze
        # processes members in group/chain order, and a later member's
        # partial clamp (its route anchors) can re-break a tie an earlier
        # member already froze at the agreed value (HECA #256: G froze at
        # the consensus ~100.8, then T's d=302 anchor clamped T@217 up to
        # 103.3 — the 18 m tie ended 2.4 m violated and the two chains wrote
        # a wall across one junction ring edge).  Re-project frozen member
        # pairs into their tie windows, each move bounded by the member's
        # OWN other anchors at the taxi cap — headroom decides who yields
        # (G had 2.5 m of ceiling room; T had none).
        if WRITE_ARBITRATION and edges_r:
            def _feas_excl9(ci, k):
                cd9 = chain_data[ci]
                sts9 = cd9["stations"]
                lo9, hi9 = float("-inf"), float("inf")
                for j9 in range(len(sts9)):
                    if j9 == k or not cd9["anchored"][j9]:
                        continue
                    lim9 = (TAXI_MAX_GRADE
                            * abs(sts9[k]["d"] - sts9[j9]["d"]) + 0.02)
                    lo9 = max(lo9, cd9["elevs"][j9] - lim9)
                    hi9 = min(hi9, cd9["elevs"][j9] + lim9)
                return lo9, hi9

            n_rec = 0
            for _sw9 in range(30):
                worst9 = 0.0
                for ((ca9, ka9), (cb9, kb9), ln9) in edges_r:
                    cda9 = chain_data[ca9]
                    cdb9 = chain_data[cb9]
                    if not (cda9["anchored"][ka9] and cdb9["anchored"][kb9]):
                        continue
                    ha9 = cda9["hard"][ka9] or _sv_chain(ca9)
                    hb9 = cdb9["hard"][kb9] or _sv_chain(cb9)
                    if ha9 and hb9:
                        continue
                    lim9 = TAXI_MAX_GRADE * ln9 + 0.02
                    ea9 = cda9["elevs"][ka9]
                    eb9 = cdb9["elevs"][kb9]
                    ex9 = abs(ea9 - eb9) - lim9
                    if ex9 <= 0.01:
                        continue
                    sgn9 = 1.0 if ea9 > eb9 else -1.0
                    fa9 = _feas_excl9(ca9, ka9)
                    fb9 = _feas_excl9(cb9, kb9)
                    na9, nb9 = ea9, eb9
                    if not ha9:
                        na9 = min(max(ea9 - sgn9 * ex9 / 2.0, fa9[0]),
                                  fa9[1])
                    if not hb9:
                        nb9 = min(max(eb9 + sgn9 * ex9 / 2.0, fb9[0]),
                                  fb9[1])
                    # residual after the half-split clamps goes to whichever
                    # side still has headroom
                    rem9 = abs(na9 - nb9) - lim9
                    if rem9 > 0.0:
                        if not ha9:
                            na9 = min(max(nb9 + sgn9 * lim9, fa9[0]), fa9[1])
                        rem9 = abs(na9 - nb9) - lim9
                        if rem9 > 0.0 and not hb9:
                            nb9 = min(max(na9 - sgn9 * lim9, fb9[0]),
                                      fb9[1])
                    if (na9, nb9) != (ea9, eb9):
                        cda9["elevs"][ka9] = na9
                        cdb9["elevs"][kb9] = nb9
                        n_rec += 1
                        worst9 = max(worst9, ex9)
                        if (_os.environ.get("O4_CORR_RECDBG") == "1"
                                and _sw9 == 0 and ex9 > 0.5):
                            ra9 = sorted({rects[ri]["shape"].ref or "?"
                                          for (ri, _n, _f) in cda9["chain"]})
                            rb9 = sorted({rects[ri]["shape"].ref or "?"
                                          for (ri, _n, _f) in cdb9["chain"]})
                            print(f"[corr]   reconcile {ra9}@k{ka9} "
                                  f"{ea9:.2f}->{na9:.2f} | {rb9}@k{kb9} "
                                  f"{eb9:.2f}->{nb9:.2f} ln={ln9:.0f}")
                if worst9 < 0.02:
                    break
            if _os.environ.get("O4_CORRIDOR_DEBUG") == "1" and n_rec:
                print(f"[corr] post-freeze tie reconcile: {n_rec} move(s)")

        if _os.environ.get("O4_CORRIDOR_DEBUG") == "1":
            for i in sorted(set(dem_hi) | set(dem_lo)):
                print(f"[corr]   runway-flex demand n{i} "
                      f"ref={rwy_ref_of.get(i, '?')!r} "
                      f"cur={elev[i]:.2f} "
                      f"lo={dem_lo.get(i)} hi={dem_hi.get(i)}")
        if _os.environ.get("O4_CORRIDOR_DEBUG") == "1" and g_members:
            print(f"[corr] joint network: {len(g_members)} tie group(s) / "
                  f"{sum(len(mm) for mm in g_members)} station(s), "
                  f"{len(g_edges)} cap tie(s), rounds={n_rounds}, "
                  f"freeze-skipped={n_skip}")

        # SAME-APRON TERMINUS PROJECTION (post-freeze): split each pair's
        # over-cap excess between the two ends, each clamped by its own
        # chain's HARD-anchor feasibility; if the pair still can't reach,
        # nothing moves (honest conflict — the no-move-if-unreachable rule).
        def _hard_feas9(cd9, k9):
            # feasibility vs EVERY other anchor of the own chain (hard AND
            # frozen ties): a terminus already at cap against a frozen tie
            # has zero headroom — the partner end absorbs the whole move.
            lo9, hi9 = float("-inf"), float("inf")
            sts9 = cd9["stations"]
            for j9 in range(len(sts9)):
                if j9 == k9 or not cd9["anchored"][j9]:
                    continue
                dd9 = abs(sts9[k9]["d"] - sts9[j9]["d"])
                lo9 = max(lo9, cd9["elevs"][j9] - TAXI_MAX_GRADE * dd9 - 0.04)
                hi9 = min(hi9, cd9["elevs"][j9] + TAXI_MAX_GRADE * dd9 + 0.04)
            return lo9, hi9

        for (ca9, ka9, cb9, kb9, d9) in apron_end_pairs:
            ca9, ka9 = _resolve(("s", ca9, ka9))
            cb9, kb9 = _resolve(("s", cb9, kb9))
            cda, cdb = chain_data[ca9], chain_data[cb9]
            if not (cda["anchored"][ka9] and cdb["anchored"][kb9]):
                continue            # consensus-managed elsewhere
            if cda["hard"][ka9] or cdb["hard"][kb9]:
                continue
            ea, eb = cda["elevs"][ka9], cdb["elevs"][kb9]
            lim9 = TAXI_MAX_GRADE * d9 + 0.05
            ex9 = abs(ea - eb) - lim9
            if ex9 <= 0.0:
                continue
            sgn9 = 1.0 if ea > eb else -1.0
            la9, ha9 = _hard_feas9(cda, ka9)
            lb9, hb9 = _hard_feas9(cdb, kb9)
            na9 = min(max(ea - sgn9 * (ex9 / 2.0 + 0.01), la9), ha9)
            nb9 = min(max(eb, na9 - lim9 + 0.01), na9 + lim9 - 0.01)
            nb9 = min(max(nb9, lb9), hb9)
            if abs(na9 - nb9) > lim9:
                continue            # unreachable — leave both untouched
            if _os.environ.get("O4_CORRIDOR_DEBUG") == "1":
                ra9 = sorted({rects[ri]["shape"].ref or "?"
                              for (ri, _n, _f) in cda["chain"]})
                rb9 = sorted({rects[ri]["shape"].ref or "?"
                              for (ri, _n, _f) in cdb["chain"]})
                print(f"[corr]   apron-pair project {ra9}@d="
                      f"{cda['stations'][ka9]['d']:.0f} {ea:.2f}->{na9:.2f}"
                      f" | {rb9}@d={cdb['stations'][kb9]['d']:.0f} "
                      f"{eb:.2f}->{nb9:.2f} (sep {d9:.0f} m)")
            cda["elevs"][ka9] = na9
            cdb["elevs"][kb9] = nb9

    # ── per chain: smooth ROUTE-BANDED profile through the (now agreed)
    # anchors + writeback.  Junction interiors are NOT written per-chain:
    # crossings and mouth values are RECORDED, and a final TWIST pass
    # blends them (user model: rects slope in ONE direction; the
    # junction's arms twist from the flat cross-sections at the mouths to
    # the max COMPOUND slope at the center and back out the other sides).
    written: set = set()
    crossings: dict = {}                       # junction idx → line sources
    jpoints: dict = {}                         # junction idx → point sources
    # NEAR-COINCIDENT TWINS: distinct canonical nodes ≤0.1 m apart (just
    # past the weld tolerance) carry ONE surface — first writer wins
    # ACROSS twins, else two chains' writes 0.2 m apart at d=0.00 read as
    # an infinite-grade cross-shape step (HECA W2↔W3 / L↔#293 / E↔#320).
    twin: dict = {}
    if nodes is not None:
        cellt: dict = {}
        for i2, (x2, y2) in enumerate(nodes):
            cellt.setdefault((int(x2 // 1.0), int(y2 // 1.0)),
                             []).append(i2)
        for i2, (x2, y2) in enumerate(nodes):
            tw = [j2
                  for dx2 in (-1, 0, 1) for dy2 in (-1, 0, 1)
                  for j2 in cellt.get((int(x2 // 1.0) + dx2,
                                       int(y2 // 1.0) + dy2), ())
                  if j2 != i2
                  and (x2 - nodes[j2][0]) ** 2
                  + (y2 - nodes[j2][1]) ** 2 <= 0.01]
            if tw:
                twin[i2] = tw

    # write tracing: O4_TRACE_LL="lat,lon;lat,lon" prints every corridor
    # write touching nodes within 1.5 m of the given points
    tr_set: set = set()
    tr_ctx = ["?"]
    if _os.environ.get("O4_TRACE_LL") and nodes is not None:
        for part9 in _os.environ["O4_TRACE_LL"].split(";"):
            try:
                la9, lo9 = (float(x) for x in part9.split(","))
                xq, yq = layout.ll_to_m(la9, lo9)
            except _GEOM_EXC:
                continue
            for i9, (xn9, yn9) in enumerate(nodes):
                if (xn9 - xq) ** 2 + (yn9 - yq) ** 2 <= 2.25:
                    tr_set.add(i9)
        print(f"[trace] corridor write-trace armed for nodes "
              f"{sorted(tr_set)}")

    def _write(i, v):
        v0 = v
        for j2 in twin.get(i, ()):
            if j2 in written:
                v = elev[j2]               # first writer wins across twins
                break
        if i in tr_set:
            print(f"[trace]   write n{i}={v:.2f}"
                  + (f" (twin override of {v0:.2f})" if v != v0 else "")
                  + f" ctx={tr_ctx[0]}")
        elev[i] = v
        written.add(i)
        # a sloping rect's END-PAIR corners must stay CO-LEVEL (the
        # X-Plane plane emit carries ONE altitude per end — the twist
        # writing them individually left HECA stub L's low end 111.4 vs
        # 111.7, which the emit averaged into a 0.2 m cross-shape step)
        for j2 in (coupling.get(i, ()) if coupling else ()):
            if j2 != i and j2 not in written and not base_hard[j2]:
                if j2 in tr_set:
                    print(f"[trace]   write n{j2}={v:.2f} ctx=couple<-n{i}")
                elev[j2] = v
                written.add(j2)

    n_chains = 0
    for cd in chain_data:
        chain = cd["chain"]
        stations = cd["stations"]
        mouth_st = cd["mouth_st"]
        L = cd["L"]
        elevs = cd["elevs"]
        anchored = cd["anchored"]
        st_lo, st_hi = cd["st_lo"], cd["st_hi"]
        fractions = [st["d"] / L for st in stations]
        pre = list(elevs)
        # FLAT SEED (the runway profile model applied to corridors):
        # interior stations take the piecewise-linear interpolation
        # between consecutive ANCHORED stations — the flattest profile
        # through the pins, DEM lowest priority.  ``faa_joint_solve`` is
        # a feasibility PROJECTOR, not a smoother: fed the incoming
        # DEM-following values it keeps any cap-legal V-notch (HECA T:
        # 105.0 → 104.5 → 111.7 through junction -10292 was Δg-legal
        # because the flanking segments are hundreds of metres long).
        # Beyond the outermost pins the seed extends FLAT (a tied
        # terminus is no longer an anchor; its stretch follows the pin).
        ai = [k for k in range(len(stations)) if anchored[k]]
        for k in range(ai[0]):
            elevs[k] = elevs[ai[0]]
        for k in range(ai[-1] + 1, len(stations)):
            elevs[k] = elevs[ai[-1]]
        for a, b in zip(ai, ai[1:]):
            da, db = stations[a]["d"], stations[b]["d"]
            if db - da < 1e-9:
                continue
            for k in range(a + 1, b):
                t = (stations[k]["d"] - da) / (db - da)
                elevs[k] = elevs[a] + t * (elevs[b] - elevs[a])
        # ROUTE-BAND THREADING: clamp the seed into each station's
        # runway-route band, then solve and iteratively anchor the worst
        # band violation (the runway bounded-re-smooth pattern) so the
        # profile respects every route demand crossing the corridor —
        # with an anchor-consistency guard at the taxi cap (the s73
        # runway lesson: an anchor added later can be jointly infeasible
        # with earlier ones).
        if st_lo is not None:
            for k in range(len(stations)):
                if not anchored[k]:
                    elevs[k] = min(max(elevs[k], st_lo[k]), st_hi[k])
        banned: set = set()
        for _round in range(7):
            faa_joint_solve(fractions, elevs, anchored, L,
                            grade_cap=TAXI_MAX_GRADE,
                            max_dg_per_m=TAXIWAY_MAX_GRADE_CHANGE_PER_M,
                            end_grade_cap=None)
            if st_lo is None:
                break
            worst_k = -1
            worst_ex = 0.05
            for k in range(len(stations)):
                if anchored[k] or k in banned:
                    continue
                ex = max(elevs[k] - st_hi[k], st_lo[k] - elevs[k])
                if ex > worst_ex:
                    worst_ex = ex
                    worst_k = k
            if worst_k < 0:
                break
            v = min(max(elevs[worst_k], st_lo[worst_k]), st_hi[worst_k])
            ok = True
            for j in range(len(stations)):
                if not anchored[j]:
                    continue
                dd = abs(stations[worst_k]["d"] - stations[j]["d"])
                if abs(v - elevs[j]) > TAXI_MAX_GRADE * dd + 0.02:
                    ok = False
                    break
            if not ok:
                banned.add(worst_k)
                continue
            elevs[worst_k] = v
            anchored[worst_k] = True
        n_chains += 1
        if _os.environ.get("O4_CORRIDOR_DEBUG") == "1":
            refs = [(rects[ri]["shape"].ref or rects[ri]["shape"].role)
                    for (ri, _n, _f) in chain]
            print(f"[corr] chain L={L:.0f} {refs}")
            print("       " + " ".join(
                f"{st['d']:.0f}:{p:.1f}->{e:.1f}"
                + ("H" if any(base_hard[i9] for i9 in st["nodes"]) else "")
                + ("R" if st["nodes"] & rwy_nodes else "")
                + ("A" if a else "")
                for st, p, e, a in zip(stations, pre, elevs, anchored)))
        # SKEWED-RECT END-PAIR CAP (post-solve, pre-write): the banded
        # solve caps a rect's profile drop over the MOUTH-MID distance,
        # but the emitted plane's binding run is the SHORTEST LONG EDGE —
        # a rect with skewed ends carries the full end-to-end delta over
        # less distance than the span (CYXY stub A: 0.81 m legal over
        # the ~50 m span but its long edge is 48.9 m → 1.74 % on the
        # emitted surface, the airport's last per-axis violation).
        # Clamp each rect's mouth-station pair minimally: free ends
        # move first; frozen-tie pairs split (the small post-freeze
        # projection is the established pattern); hard ends never move.
        for (ri9, nmi9, fmi9), (kn9, kf9) in zip(chain, mouth_st):
            r9 = rects[ri9]
            na_set = r9["mouths"][nmi9]["nodes"]
            nb_set = r9["mouths"][fmi9]["nodes"]
            idxs9 = r9["idxs"]
            ring9 = r9["ring"]
            m9 = len(idxs9)
            if not m9 or len(ring9) < m9:
                continue
            min_le = None
            for a9 in range(m9):
                ia9, ib9 = idxs9[a9], idxs9[(a9 + 1) % m9]
                if ia9 is None or ib9 is None:
                    continue
                if ((ia9 in na_set and ib9 in nb_set)
                        or (ia9 in nb_set and ib9 in na_set)):
                    (xa9, ya9) = ring9[a9]
                    (xb9, yb9) = ring9[(a9 + 1) % m9]
                    le9 = math.hypot(xa9 - xb9, ya9 - yb9)
                    if min_le is None or le9 < min_le:
                        min_le = le9
            if min_le is None:
                continue
            lim9 = TAXI_MAX_GRADE * max(min_le, 2.0) + 0.04
            ea9, eb9 = elevs[kn9], elevs[kf9]
            ex9 = abs(ea9 - eb9) - lim9
            if ex9 <= 0.0:
                continue
            ha9 = cd["hard"][kn9]
            hb9 = cd["hard"][kf9]
            if ha9 and hb9:
                continue
            sgn9 = 1.0 if ea9 > eb9 else -1.0
            if ha9:
                elevs[kf9] = eb9 + sgn9 * ex9
            elif hb9:
                elevs[kn9] = ea9 - sgn9 * ex9
            else:
                elevs[kn9] = ea9 - sgn9 * ex9 / 2.0
                elevs[kf9] = eb9 + sgn9 * ex9 / 2.0
            if _os.environ.get("O4_CORRIDOR_DEBUG") == "1":
                print(f"[corr]   skew end-pair cap "
                      f"{r9['shape'].ref or '?'} min_edge={min_le:.0f} "
                      f"({ea9:.2f},{eb9:.2f}) -> "
                      f"({elevs[kn9]:.2f},{elevs[kf9]:.2f})")
        # station nodes take the profile (first corridor to write a node
        # wins — longer corridors are processed first via the seed order).
        if tr_set:
            tr_ctx[0] = "station " + "/".join(sorted(
                {rects[ri]["shape"].ref or "?" for (ri, _n, _f) in chain}))
        for st, e in zip(stations, elevs):
            for i in st["nodes"]:
                if not base_hard[i] and i not in written:
                    _write(i, e)
                elif i in tr_set:
                    print(f"[trace]   SKIP n{i} station v={e:.2f} "
                          f"(hard={base_hard[i]} "
                          f"already={elev[i]:.2f})")
        st_ds = [st["d"] for st in stations]
        # rect bodies: every ring vertex interpolates axially between its
        # rect's two mouth stations (keeps inserted shared-edge vertices —
        # the apron seams — on the corridor plane).
        for (ri, nmi, fmi), (kn, kf) in zip(chain, mouth_st):
            r = rects[ri]
            pn = r["mouths"][nmi]["p"]
            pf = r["mouths"][fmi]["p"]
            span = pf - pn
            if abs(span) < 1e-6:
                continue
            en, ef = elevs[kn], elevs[kf]
            if tr_set:
                tr_ctx[0] = (f"body {r['shape'].ref or '?'} "
                             f"{en:.2f}->{ef:.2f}")
            for k, i in enumerate(r["idxs"]):
                if i is None or base_hard[i] or i in written:
                    if i is not None and i in tr_set:
                        print(f"[trace]   SKIP n{i} body "
                              f"{r['shape'].ref or '?'} "
                              f"(hard={base_hard[i]} "
                              f"already={elev[i]:.2f})")
                    continue
                t = (r["projs"][k] - pn) / span
                t = min(max(t, 0.0), 1.0)
                _write(i, en + t * (ef - en))
        # RECORD junction sources for the twist pass: each crossing as a
        # LINE source (the corridor's profile along its crossing segment),
        # each mouth touching a junction as a POINT source (its station
        # value — keeps the arms anchored to their flat cross-sections).
        for el, (kn, kf) in zip(chain, mouth_st):
            ri, nmi, fmi = el
            for mi2, kk in ((nmi, kn), (fmi, kf)):
                m = rects[ri]["mouths"][mi2]
                ji = m.get("junc")
                if ji is not None:
                    jpoints.setdefault(ji, []).append(
                        (m["mid"], elevs[kk]))
        for (a, b) in zip(range(len(chain) - 1), range(1, len(chain))):
            ri, nmi, fmi = chain[a]
            rj, nmj, fmj = chain[b]
            mA = rects[ri]["mouths"][fmi]
            mB = rects[rj]["mouths"][nmj]
            ji = mA.get("junc")
            if ji is None or ji != mB.get("junc"):
                continue
            dA = stations[mouth_st[a][1]]["d"]
            dB = stations[mouth_st[b][0]]["d"]
            if (mB["mid"][0] - mA["mid"][0]) ** 2 \
                    + (mB["mid"][1] - mA["mid"][1]) ** 2 < 4.0:
                continue
            crossings.setdefault(ji, []).append({
                "A": mA["mid"], "B": mB["mid"], "dA": dA, "dB": dB,
                "ds": list(st_ds), "es": list(elevs)})
        # virtual runway-exit segments paint their junction too: the
        # twist line source runs from the runway vertex to the first
        # mouth — the exit junction carries the climb (HECA #282)
        for end2 in (0, 1):
            k4 = 0 if end2 == 0 else len(stations) - 1
            st4 = stations[k4]
            if not st4.get("virtual") or st4.get("junc") is None:
                continue
            step4 = 1 if end2 == 0 else -1
            kn4 = k4 + step4
            while (0 <= kn4 < len(stations)
                   and stations[kn4]["mid"] is None):
                kn4 += step4
            if not 0 <= kn4 < len(stations):
                continue
            crossings.setdefault(st4["junc"], []).append({
                "A": st4["mid"], "B": stations[kn4]["mid"],
                "dA": st4["d"], "dB": stations[kn4]["d"],
                "ds": list(st_ds), "es": list(elevs)})
            jpoints.setdefault(st4["junc"], []).append(
                (st4["mid"], elevs[k4]))

    # ── TWIST PASS (user model 2026-06-10): junction vertices blend the
    # crossing corridors' profiles by inverse-square distance — near a
    # mouth the local corridor dominates (flat cross-section matching the
    # rect), at the center the crossings superpose into the max COMPOUND
    # slope, and the arms twist smoothly between.  Point sources (mouth
    # station values) keep arms serving non-crossing corridors anchored.
    tw_all: set = set()                 # twist writes across all junctions
    for ji, xs in crossings.items():
        J = juncs[ji]
        pts = jpoints.get(ji, ())
        tw_set: set = set()             # this junction's twist writes
        for k, i in enumerate(J["idxs"]):
            if i is None or base_hard[i] or i in written:
                continue
            x, y = J["ring"][k]
            srcs = []
            for c in xs:
                Ax, Ay = c["A"]
                Bx, By = c["B"]
                gl2 = (Bx - Ax) ** 2 + (By - Ay) ** 2
                t = ((x - Ax) * (Bx - Ax) + (y - Ay) * (By - Ay)) / gl2
                t = min(max(t, 0.0), 1.0)
                px, py = Ax + t * (Bx - Ax), Ay + t * (By - Ay)
                lat = math.hypot(x - px, y - py)
                v = _interp_profile(c["ds"], c["es"],
                                    c["dA"] + t * (c["dB"] - c["dA"]))
                srcs.append((1.0 / (lat + 3.0) ** 2, v))
            for (pm, pv) in pts:
                lat = math.hypot(x - pm[0], y - pm[1])
                srcs.append((1.0 / (lat + 3.0) ** 2, pv))
            den = sum(w for w, _v in srcs)
            if den <= 0.0:
                continue
            mean = sum(w * v for w, v in srcs) / den
            # DISAGREEMENT GUARD: when the weight-dominant sources
            # genuinely conflict (two corridors' profiles several metres
            # apart NEAR this vertex — junction -10193 read a 5.5 m blend
            # artifact), averaging manufactures a surface neither corridor
            # wants; leave the vertex to the enforcement instead.
            var = sum(w * (v - mean) ** 2 for w, v in srcs) / den
            if var > 1.0:
                continue
            # clamp into the junction's HARD band (a blend that ignores a
            # runway vertex a chord away manufactures the grade the
            # enforce cannot fix — same rule as the station bands).  The
            # per-vertex ROUTE bands are deliberately NOT applied here:
            # inside a corridor-crossed junction the corridor profile IS
            # the route truth and the per-vertex route bands are the
            # noisy approximation (route-graph artifacts lift adjacent
            # vertices metres apart) — these junctions are instead
            # EXEMPTED from band pinning in the final enforce.
            lo, hi = float("-inf"), float("inf")
            for (hp, he, _hi2) in jhard.get(ji, ()):
                dd = _junc_geo_dist(ji, (x, y), hp)
                if dd is None:
                    continue
                lo = max(lo, he - TAXI_MAX_GRADE * dd - 0.02)
                hi = min(hi, he + TAXI_MAX_GRADE * dd + 0.02)
            if lo > hi:
                continue
            if tr_set:
                tr_ctx[0] = (f"twist ji={ji} mean={mean:.2f} "
                             f"band=[{lo:.2f},{hi:.2f}]")
            _write(i, min(max(mean, lo), hi))
            tw_set.add(i)
            tw_all.add(i)
        # LOCAL CONSISTENCY SWEEP: the inverse-square blend leaves two
        # LOW-variance vertices near different sources metres apart (the
        # per-vertex disagreement guard cannot see the pair — SPJC #107
        # read 1.4 m / 24 m internally).  Cap-project the twist writes
        # against each other and the held ring values over the in-polygon
        # geodesics; mouths/stations/hard stay fixed.
        if tw_set:
            tab2 = _junc_geo_table(ji)
            idxs2 = J["idxs"]
            for _sw in range(30):
                worst2 = 0.0
                for a in range(len(idxs2)):
                    ia = idxs2[a]
                    if ia is None or not (ia in written
                                          or base_hard[ia]):
                        continue
                    for b in range(a + 1, len(idxs2)):
                        ib = idxs2[b]
                        if ib is None or not (ib in written
                                              or base_hard[ib]):
                            continue
                        fa = ia in tw_set
                        fb = ib in tw_set
                        if not (fa or fb):
                            continue
                        dd2 = tab2[a][b]
                        if dd2 == float("inf"):
                            continue
                        lim2 = TAXI_MAX_GRADE * dd2 + 0.02
                        diff2 = elev[ia] - elev[ib]
                        ex2 = abs(diff2) - lim2
                        if ex2 <= 0.0:
                            continue
                        sgn2 = 1.0 if diff2 > 0 else -1.0
                        if fa and fb:
                            elev[ia] -= sgn2 * ex2 / 2.0
                            elev[ib] += sgn2 * ex2 / 2.0
                        elif fa:
                            elev[ia] -= sgn2 * ex2
                        else:
                            elev[ib] += sgn2 * ex2
                        worst2 = max(worst2, ex2)
                if worst2 < 0.01:
                    break
            # re-level any rect end-pair the sweep split (the plane emit
            # carries one altitude per end)
            if coupling:
                done2: set = set()
                for i3 in sorted(tw_set):
                    grp = coupling.get(i3)
                    if not grp or grp in done2:
                        continue
                    done2.add(grp)
                    vals3 = [elev[j3] for j3 in grp if j3 in written]
                    if not vals3:
                        continue
                    vmean = sum(vals3) / len(vals3)
                    for j3 in grp:
                        if not base_hard[j3] and j3 in written:
                            elev[j3] = vmean
    # COINCIDENT-NODE SYNC: distinct canonical nodes a few centimetres
    # apart (just past the weld tolerance) must carry ONE surface — a
    # corridor write on one twin and not the other reads as an
    # infinite-grade cross-shape step (HECA W2↔W3 / L↔#293: 0.2 m at
    # d=0.00 m).  The unwritten twin takes the written value and is held.
    if nodes is not None and written:
        cell2: dict = {}
        for i2 in range(len(nodes)):
            x2, y2 = nodes[i2]
            cell2.setdefault((int(x2 // 1.0), int(y2 // 1.0)),
                             []).append(i2)
        for i2 in sorted(written):
            if i2 >= len(nodes):
                continue
            x2, y2 = nodes[i2]
            for dx2 in (-1, 0, 1):
                for dy2 in (-1, 0, 1):
                    for j2 in cell2.get((int(x2 // 1.0) + dx2,
                                         int(y2 // 1.0) + dy2), ()):
                        if (j2 == i2 or j2 in written
                                or base_hard[j2]):
                            continue
                        xj, yj = nodes[j2]
                        if (x2 - xj) ** 2 + (y2 - yj) ** 2 <= 0.01:
                            _write(j2, elev[i2])

    # corridor-touched junctions are EXEMPT from per-vertex route-band
    # pinning in the final enforce: the threaded corridor profile is the
    # route truth there; the per-vertex bands (route-graph artifacts)
    # otherwise hold free junction vertices metres above held corridor
    # writes — the part-4 cliff class (HECA #290: free 104.0 against
    # held 102.1, 7.4 m apart, unfixable by POCS).
    exempt: set = set()
    for ji in set(j_sts) | set(crossings):
        for i in juncs[ji]["idxs"]:
            if i is not None and not base_hard[i]:
                exempt.add(i)
    # Free APRON vertices near a held corridor write are the same
    # band-artifact class: the corridor threaded its OWN route bands, but
    # the per-vertex reach graph disagrees by route noise (~0.02 % over a
    # 3 km route = 0.6 m at HECA #190) and pins the apron edge above the
    # held write — POCS then can't conform the apron to the corridor.
    # Exempt apron vertices within 60 m of a held write (measured pairs
    # sit 8-50 m out) and let the visibility-edge projection finish.
    if apron_nodes and nodes is not None:
        wcell: dict = {}
        _R2 = 60.0
        for i in written:
            if base_hard[i]:
                continue
            xw, yw = nodes[i]
            wcell.setdefault((int(xw // _R2), int(yw // _R2)),
                             []).append((xw, yw))
        for i in apron_nodes:
            if i in written or base_hard[i] or i in exempt:
                continue
            xa, ya = nodes[i]
            ca, cb = int(xa // _R2), int(ya // _R2)
            near9 = False
            for dxc in (-1, 0, 1):
                for dyc in (-1, 0, 1):
                    for (xw, yw) in wcell.get((ca + dxc, cb + dyc), ()):
                        if (xa - xw) ** 2 + (ya - yw) ** 2 <= _R2 * _R2:
                            near9 = True
                            break
                    if near9:
                        break
                if near9:
                    break
            if near9:
                exempt.add(i)
    # TWIST writes are a SEED, not a hold: the corridor-touched junctions
    # are band-exempt in the enforce, so the full POCS (which knows the
    # bridge rects, visibility chords and every neighbouring shape the
    # twist cannot see) finishes the junction interiors — holding them
    # left cross-junction conflicts the enforce could never close (SPJC's
    # V/Q/R complex: R2 bridge read 1.4 m between two held twist flanks).
    held_out = written - tw_all
    if _os.environ.get("O4_STEP_DEBUG") == "1":
        print(f"[step] corridor profiles: {n_chains} multi-rect corridor(s), "
              f"{len(written)} node(s) written ({len(held_out)} held, "
              f"{len(tw_all)} twist-seeded free), "
              f"{len(exempt)} junction node(s) band-exempt, "
              f"{len(set(dem_lo) | set(dem_hi))} runway-flex demand(s)")
    if tr_set:
        for i9 in sorted(tr_set):
            print(f"[trace] corridor END n{i9}={elev[i9]:.2f} "
                  f"written={i9 in written} twist={i9 in tw_all} "
                  f"held={i9 in held_out}")
    return held_out, exempt, (dem_lo, dem_hi, dem_refs)


# ── APRON EDGE RETREAT (user ruling 2026-06-11, HECA #198) ─────────
# An apron edge ROUTE-PINNED metres above the apron's own field
# equilibrium (shared corners with a junction/lane system graded from a
# runway the apron does not serve through that edge) must not drag the
# apron up: physically such edges are terrain breaks (HECA #198: a
# switchback road climbs between the apron and taxiway S — the user
# wants the apron flat-low at ~104 with "a bit of a cliff along that
# edge", where the surface held 107.9-108.2).  Post-writeback: lower
# the pinned edge verts to their LOCAL FIELD value, and where such a
# vert is welded to (or hugs) another airside shape, RETREAT it inward
# so the weld breaks — the strip between becomes non-airside and the
# boundary/clearance machinery renders the cliff face against the
# high neighbour.  The neighbour keeps its solved values.
_RETREAT_PIN_M = 2.0          # pinned-above-field threshold
_RETREAT_FIELD_GAP_M = 120.0  # the field reference must be local
_RETREAT_BODY_TOL_M = 1.2     # apron body must be field-consistent
_RETREAT_IN_M = 10.0          # inward move for welded/hugging verts
_RETREAT_HUG_M = 6.0          # closer than this to another shape = hug


def _retreat_route_pinned_apron_edges(layout, dem_ctx=None,
                                      bucket_to_idx=None) -> int:
    """See block comment above.  Mutates apron polygons/node_altitudes
    in place; returns the number of verts adjusted."""
    F = getattr(layout, "_network_profile_field", None)
    if F is None:
        return 0
    dem_at = None
    if dem_ctx is not None and dem_ctx[0] is not None:
        _dem9, _tla9, _tlo9 = dem_ctx
        from auto_patch.elevation import _sample_dem as _rsd

        def dem_at(x9, y9):
            try:
                la9, lo9 = layout.m_to_ll(x9, y9)
                e9 = _rsd(_dem9, _tla9, _tlo9, la9, lo9)
            except _GEOM_EXC:
                return None
            return float(e9) if e9 is not None else None
    from shapely.geometry import Polygon as _RPoly, Point as _RPt
    # other airside shapes' edges for the hug test
    airside = [s for s in layout.shapes
               if s.role in PAVEMENT_ROLES and s.polygon is not None
               and not s.polygon.is_empty]
    n_moved = 0
    dbg = _os.environ.get("O4_RETREAT_DEBUG") == "1"
    from shapely.prepared import prep as _rprep
    for s in layout.shapes:
        if s.role != ROLE_APRON or s.polygon is None or s.polygon.is_empty:
            continue
        na = list(s.node_altitudes or [])
        if not na:
            continue
        ring = list(s.polygon.exterior.coords)
        closed = len(ring) > 1 and ring[0] == ring[-1]
        pts = ring[:-1] if closed else ring
        m = len(pts)
        if m < 4 or len(na) < m:
            continue
        # the equilibrium reference = the apron's OWN INTERIOR lanes —
        # F.sample's nearest edge at a rim vert is often the HIGH
        # neighbour's lane across the very gap we're breaking (HECA
        # #198: rim verts read taxiway S's 107.4 across the road gap
        # while the apron's B/C lanes inside say ~104)
        try:
            pp9 = _rprep(s.polygon.buffer(3.0))
        except _GEOM_EXC:
            continue
        own_segs = []
        for (ia9, ib9) in F._segs:
            (ax9, ay9) = F.nodes[ia9]
            (bx9, by9) = F.nodes[ib9]
            try:
                if pp9.contains(_RPt(((ax9 + bx9) / 2.0,
                                      (ay9 + by9) / 2.0))):
                    own_segs.append((ia9, ib9))
            except _GEOM_EXC:
                continue
        if not own_segs:
            continue            # no interior lanes: no equilibrium proof

        def _own_sample(x9, y9):
            best9 = (float("inf"), None)
            for (ia9, ib9) in own_segs:
                (ax9, ay9) = F.nodes[ia9]
                (bx9, by9) = F.nodes[ib9]
                dx9, dy9 = bx9 - ax9, by9 - ay9
                s29 = dx9 * dx9 + dy9 * dy9
                if s29 < 1e-12:
                    continue
                t9 = ((x9 - ax9) * dx9 + (y9 - ay9) * dy9) / s29
                t9 = 0.0 if t9 < 0.0 else (1.0 if t9 > 1.0 else t9)
                d9 = math.hypot(x9 - (ax9 + t9 * dx9),
                                y9 - (ay9 + t9 * dy9))
                if d9 < best9[0]:
                    best9 = (d9, F.elev[ia9]
                             + t9 * (F.elev[ib9] - F.elev[ia9]))
            return best9[1], best9[0]

        # apron-wide interior-lane MEDIAN (a polluted crossing lane —
        # apt.dat drawing a lane over the road gap — drags the NEAREST
        # sample up; the median across all interior lanes stays at the
        # apron's true level)
        med_f = None
        mv9 = sorted(F.elev[i9] for (ia9, ib9) in own_segs
                     for i9 in (ia9, ib9))
        if mv9:
            med_f = mv9[len(mv9) // 2]
        # the apron's DEM median — the terrain-clause agreement test
        # compares apron-wide medians (a big sloped apron's LOCAL DEM
        # at a rim differs from the interior median by metres while
        # both medians agree)
        med_dem = None
        if dem_at is not None:
            dv9 = sorted(v9 for v9 in (dem_at(x9, y9)
                                       for (x9, y9) in pts)
                         if v9 is not None)
            if dv9:
                med_dem = dv9[len(dv9) // 2]
        # field reference + pin classification per vert
        fvals = []
        pins = []
        for k, (x, y) in enumerate(pts):
            fv, gap = _own_sample(x, y)
            ok = fv is not None and gap <= _RETREAT_FIELD_GAP_M
            a = na[k]
            pin = ok and a is not None and a - fv >= _RETREAT_PIN_M
            tgt = fv if ok else None
            if not pin and a is not None and dem_at is not None \
                    and med_f is not None:
                # TERRAIN clause: surface ≥2.5 m above the DEM while the
                # apron's interior lanes agree with the DEM — the rim is
                # externally imposed (HECA #198: rim 108.1, DEM 104.3,
                # interior-lane median ~104; the wanted cliff edge)
                dv = dem_at(x, y)
                if (dv is not None and a - dv >= 2.5
                        and med_dem is not None
                        and abs(med_f - med_dem) <= 2.5):
                    pin = True
                    tgt = max(med_f, dv)
            if pin and dem_at is not None and a is not None:
                # never drop below the local terrain (the cliff toe is
                # the DEM; an own-field sample from a far lane can
                # overshoot — #198's corner read 102.2 where the DEM
                # says 104.4)
                dvf = dem_at(x, y)
                if dvf is not None:
                    tgt = min(a, max(tgt, dvf))
            fvals.append(tgt)
            pins.append(pin)
        if not any(pins):
            continue
        # the apron BODY must sit at its field / terrain (only an edge
        # zone is pinned) — a wholly-elevated apron is legitimately high.
        # Field variant OR DEM variant (far own-field samples are noisy
        # on big aprons; the DEM is the terrain-clause's own evidence).
        diffs = sorted(abs(na[k] - fvals[k]) for k in range(m)
                       if fvals[k] is not None and na[k] is not None
                       and not pins[k])
        body_ok = bool(diffs) and diffs[len(diffs) // 2] \
            <= _RETREAT_BODY_TOL_M
        if not body_ok and dem_at is not None:
            ddm = sorted(abs(na[k] - dv9b) for k in range(m)
                         if na[k] is not None and not pins[k]
                         for dv9b in (dem_at(*pts[k]),)
                         if dv9b is not None)
            body_ok = bool(ddm) and ddm[len(ddm) // 2] <= 1.5
        if not body_ok:
            if dbg and any(pins):
                print(f"  [retreat] SKIP body-test "
                      f"({sum(pins)} pinned)")
            continue
        # a terrain-break edge is a RUN of pinned verts; lone pins are
        # noise (SPJC: a single false positive moved a vert 10 m for a
        # 0.0 m value change and broke the overlap invariant).  Filters,
        # in order: every pinned vert must (i) drop ≥0.8 m and (ii) sit
        # ≥2.5 m above the local TERRAIN (the user-described signature —
        # a real cliff edge is terrain-corroborated); then the survivors
        # must form runs of ≥3 contiguous verts.
        for k in range(m):
            if not pins[k]:
                continue
            if (fvals[k] is None or na[k] is None
                    or na[k] - fvals[k] < 0.8):
                pins[k] = False
                continue
            dv9c = dem_at(*pts[k]) if dem_at is not None else None
            if dv9c is None or na[k] - dv9c < 2.5:
                pins[k] = False
                continue
            # the LAW floor: a rim lifted by a RUNWAY-anchor route
            # floor is legal lift (CYXY's route-law-raised aprons),
            # not a terrain break — the lowered value must stay legal.
            # Use the FIELD's runway-anchored bands (contacts only;
            # the enforce's dense-field floors embed the very polluted
            # coupling this pass works around).
            x9c, y9c = pts[k]
            bestf = (float("inf"), None)
            for (ia9, ib9) in own_segs:
                for i9 in (ia9, ib9):
                    d9 = math.hypot(F.nodes[i9][0] - x9c,
                                    F.nodes[i9][1] - y9c)
                    if d9 < bestf[0]:
                        bestf = (d9, i9)
            if bestf[1] is not None and F.band_lo:
                fl9 = F.band_lo[bestf[1]] - 0.0156 * bestf[0]
                if fl9 > float("-inf") and fvals[k] < fl9 - 0.05:
                    pins[k] = False
        if not any(pins):
            continue
        # apply-feasibility BEFORE run filtering: a pinned vert that is
        # welded/hugging and cannot retreat (containment) must clear its
        # pin now, or the run filter passes a run that then applies
        # PARTIALLY — 104.3 beside a kept 108.1 = a new ring step
        cen = s.polygon.centroid
        moved_to = [None] * m
        try:
            inner9 = s.polygon.buffer(-1.0)
        except _GEOM_EXC:
            inner9 = None
        for k in range(m):
            if not pins[k]:
                continue
            x, y = pts[k]
            hug = False
            P9 = _RPt((x, y))
            for s2 in airside:
                if s2 is s:
                    continue
                try:
                    if s2.polygon.exterior.distance(P9) \
                            <= _RETREAT_HUG_M:
                        hug = True
                        break
                except _GEOM_EXC:
                    continue
            if not hug:
                continue
            # inward direction = the LOCAL edge normal (the centroid ray
            # exits the polygon at concave corners — an L-shaped apron's
            # centroid sits in the other lobe); try both normal signs
            # and the centroid ray, first one that stays inside wins
            xp9, yp9 = pts[k - 1]
            xn9, yn9 = pts[(k + 1) % m]
            tx9, ty9 = xn9 - xp9, yn9 - yp9
            tl9 = math.hypot(tx9, ty9) or 1.0
            cand9 = [(-ty9 / tl9, tx9 / tl9), (ty9 / tl9, -tx9 / tl9)]
            dxc, dyc = cen.x - x, cen.y - y
            dlc = math.hypot(dxc, dyc)
            if dlc > _RETREAT_IN_M * 2:
                cand9.append((dxc / dlc, dyc / dlc))
            nv9 = None
            for (ux9, uy9) in cand9:
                t9 = (x + ux9 * _RETREAT_IN_M, y + uy9 * _RETREAT_IN_M)
                try:
                    if inner9 is not None and inner9.contains(_RPt(t9)):
                        nv9 = t9
                        break
                except _GEOM_EXC:
                    continue
            if nv9 is None:
                pins[k] = False
            else:
                moved_to[k] = nv9
        if not any(pins):
            continue
        run_ok = [False] * m
        k0 = 0
        while k0 < m:
            if not pins[k0]:
                k0 += 1
                continue
            k1 = k0
            while k1 + 1 < m and pins[k1 + 1]:
                k1 += 1
            ln9 = k1 - k0 + 1
            # circular wrap: join a run ending at m-1 with one at 0
            if k1 == m - 1 and pins[0] and k0 > 0:
                kk0 = 0
                while kk0 + 1 < m and pins[kk0 + 1]:
                    kk0 += 1
                ln9 += kk0 + 1
            if ln9 >= 3:
                for k2 in range(k0, k1 + 1):
                    run_ok[k2] = True
                if k1 == m - 1 and pins[0]:
                    k2 = 0
                    while k2 < m and pins[k2]:
                        run_ok[k2] = True
                        k2 += 1
            k0 = k1 + 1
        for k in range(m):
            if pins[k] and not run_ok[k]:
                pins[k] = False
        if not any(pins):
            continue
        new_pts = list(pts)
        new_na = list(na[:m])
        touched = []
        for k in range(m):
            if not pins[k]:
                continue
            new_na[k] = round(float(fvals[k]), 1)
            if moved_to[k] is not None:
                new_pts[k] = moved_to[k]
            touched.append(k)
        if not touched:
            continue
        try:
            np9 = _RPoly(new_pts + [new_pts[0]])
        except _GEOM_EXC:
            if dbg:
                print(f"  [retreat] SKIP poly-construct "
                      f"touched={len(touched)}")
            continue
        if (not np9.is_valid or np9.is_empty
                or np9.area < 0.85 * s.polygon.area):
            if dbg:
                print(f"  [retreat] SKIP invalid/area ring "
                      f"valid={np9.is_valid} touched={len(touched)} "
                      f"(area {np9.area:.0f} vs {s.polygon.area:.0f})")
            continue
        if dbg:
            for k in touched:
                la9, lo9 = layout.m_to_ll(*pts[k])
                print(f"  [retreat] vert@({la9:.6f},{lo9:.6f}) "
                      f"{na[k]} -> {new_na[k]}"
                      + (" (+10m inward)" if new_pts[k] != pts[k]
                         else ""))
        s.polygon = np9
        out_na = new_na + ([new_na[0]] if closed and len(na) > m else [])
        s.node_altitudes = out_na
        n_moved += len(touched)
    if n_moved:
        UI = None
        try:
            import O4_UI_Utils as UI
        except Exception:                            # pragma: no cover
            pass
        if UI is not None:
            UI.vprint(1, f"  [pav-builder] apron edge retreat: "
                         f"{n_moved} route-pinned edge vert(s) lowered "
                         f"to the local field (terrain-break edges)")
    return n_moved


def _merge_terminal_level_groups(coupling: dict, shape_constraints) -> dict:
    """Extend a ``node -> tuple(members)`` coupling with one RIGID LEVEL group per
    terminal (flat, cap-0 shape), so a terminal moves as a single flat unit in the
    final enforce projection instead of being frozen.  Flat shapes that share a
    node union into one group (edge-connected terminals co-level, session 59), and
    any existing coupling members (rect flat-pairs) that touch a terminal node
    merge in too.  Returns a fresh merged coupling dict."""
    parent: dict[int, int] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    # Seed with the existing coupling components (rect flat-end pairs).
    for node, members in coupling.items():
        for m in members:
            union(node, m)
    # Union every flat shape's nodes into one component.
    for sc in shape_constraints:
        if not sc["flat"]:
            continue
        ns = sc["nodes"]
        for k in ns[1:]:
            union(ns[0], k)
    comp: dict[int, list[int]] = {}
    for x in list(parent):
        comp.setdefault(find(x), []).append(x)
    merged: dict[int, tuple] = {}
    for members in comp.values():
        if len(members) < 2:
            continue
        t = tuple(members)
        for m in members:
            merged[m] = t
    return merged


def _reconcile_level_coupling(elev, shape_constraints, base_hard) -> int:
    """Bring every rect flat-end coupled group back to ONE elevation (the
    plane emit carries one altitude per end — see ``_build_level_coupling``).
    A hard member's value wins (hard never moves; disagreeing hards are an
    upstream conflict and are left alone).  Free groups take the mean,
    projected into the intersection of the members' EXTERNAL cap edges so
    the re-level cannot manufacture a new over-cap edge.  Returns the
    number of groups moved."""
    coupling = _build_level_coupling(shape_constraints)
    if not coupling:
        return 0
    ext: dict[int, list] = {}
    for sc in shape_constraints:
        for (i, j, c) in sc["edges"]:
            ext.setdefault(i, []).append((j, c))
            ext.setdefault(j, []).append((i, c))
    done: set = set()
    moved = 0
    for members in coupling.values():
        if id(members) in done:
            continue
        done.add(id(members))
        vals = [elev[m] for m in members]
        if max(vals) - min(vals) <= 0.02:
            continue
        hard_vals = [elev[m] for m in members if base_hard[m]]
        if hard_vals:
            if max(hard_vals) - min(hard_vals) > 0.02:
                continue       # disagreeing hard anchors — honest conflict
            target = hard_vals[0]
        else:
            target = sum(vals) / len(vals)
            lo9, hi9 = float("-inf"), float("inf")
            mset = set(members)
            for m in members:
                for (j, c) in ext.get(m, ()):
                    if j in mset:
                        continue
                    lo9 = max(lo9, elev[j] - c)
                    hi9 = min(hi9, elev[j] + c)
            if lo9 <= hi9:
                target = min(max(target, lo9), hi9)
        for m in members:
            if not base_hard[m]:
                elev[m] = target
        moved += 1
    return moved


def _build_level_coupling(shape_constraints) -> dict:
    """Build the RIGID LEVEL coupling map ``node -> tuple(members)`` (user
    2026-05-28).  Members of a group must share one elevation and move together
    under :func:`_project_shape`.  Groups = every rect flat-end cross-corner
    pair (``flat_pairs``); pairs that share a node (rect meeting rect end-to-end)
    union into one component so they stay co-levelled."""
    parent: dict[int, int] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    for sc in shape_constraints:
        for (a, b) in sc.get("flat_pairs", ()):  # type: ignore[arg-type]
            union(a, b)
    comp: dict[int, list[int]] = {}
    for x in list(parent):
        comp.setdefault(find(x), []).append(x)
    coupling: dict[int, tuple] = {}
    for members in comp.values():
        t = tuple(members)
        for m in members:
            coupling[m] = t
    return coupling


# ── Stage 1: build node list ──────────────────────────────────────


def _build_node_list(layout):
    """Assign one node index per unique canonical point across all
    pavement-role shapes.  Returns ``(nodes, bucket_to_idx)`` —
    the dict still names ``bucket_to_idx`` for legacy continuity
    but keys are canonical (x, y) tuples when the layout has a
    registry, else legacy discrete buckets.
    """
    bucket_to_idx: dict[tuple[float, float], int] = {}
    nodes: list[tuple[float, float]] = []
    for s in layout.shapes:
        if s.role not in PAVEMENT_ROLES:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = _open_ring(list(s.polygon.exterior.coords))
        except _GEOM_EXC:
            continue
        for x, y in coords:
            k = layout.canonical_points.get_or_add(float(x), float(y))
            if k not in bucket_to_idx:
                bucket_to_idx[k] = len(nodes)
                nodes.append((float(x), float(y)))
    return nodes, bucket_to_idx


def _seed_terminals_from_taxi_routes(layout, elev, bucket_to_idx, dem_elev,
                                     cap=TAXI_MAX_GRADE) -> int:
    """Pre-solve terminal SEED from taxi-route grade feasibility (user 2026-06-09).

    A terminal pad must be grade-reachable from the runways it connects to over the
    actual taxiway ROUTE (not the cross-apron straight line).  Starting it at its
    raw DEM (typically ABOVE that band) leaves the apron unable to slope down to a
    lower runway and the relief then ratchets the terminal further UP.  Seeding it
    instead at the closest-to-DEM level inside

        band = intersect over adjacent runways R of
                 [ E_R - cap*route_R , E_R + cap*route_R ]

    (``E_R`` = the runway elevation at the NEAREST connection on R — flat here, the
    seed runs before any runway flex; ``route_R`` = centerline route distance) lets
    the apron grade DOWN to the runway.  EDGE-COUPLED terminals seed as ONE flat
    unit at their COMBINED band (its midpoint when the inter-runway squeeze is
    infeasible — the residual the runway-flex / apron then absorbs).  A terminal
    not route-reachable from any runway keeps its DEM seed.  Mutates ``elev``;
    returns the number of terminals/clusters re-seeded.

    When terminals are GRADED (``TERMINAL_MAX_GRADE`` > 0) the seed is PER-NODE
    (each vertex at its own band), so a pad spanning two runways at different
    levels SLOPES — its edge near the LOW runway drops, its edge near the HIGH
    runway rises — at the ≤cap grade the span allows (HECA 6/7/10: 70 near
    05L/23R → 73 near 05C over 318 m = 0.97 %), instead of a flat pad forcing
    that tension onto the apron/runway.  When terminals are FLAT (cap 0) the
    cluster seeds as ONE level (combined band; midpoint if the squeeze is
    infeasible)."""
    from auto_patch.taxi_routing import shared_taxi_route_graph
    cps = layout.canonical_points
    G = shared_taxi_route_graph(layout)     # read-only use of the shared cache
    if not G.coord:
        return 0
    # Runway connection points: a runway node also used by a non-runway pavement
    # shape (a taxiway / junction / apron meets the runway there), with the runway
    # ref so we can take the nearest connection PER runway.
    nonrwy: set = set()
    for s in layout.shapes:
        if (s.role == ROLE_RUNWAY or s.role not in PAVEMENT_ROLES
                or s.polygon is None or s.polygon.is_empty):
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            idx = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if idx is not None:
                nonrwy.add(idx)
    rconn: list = []                    # (dist_map, src_gap, elev, ref)
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY or s.polygon is None or s.polygon.is_empty:
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            idx = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if idx is not None and idx in nonrwy:
                dm, sg = G.distances_from((x, y))
                rconn.append((dm, sg, elev[idx], s.ref or ""))
    if not rconn:
        return 0
    # Edge-coupled terminal clusters (flat cap-0 shapes unioned by shared node).
    parent: dict = {}

    def _find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # PER-NODE grade-feasible band: every terminal vertex routes from its OWN
    # position to each runway, so a vertex near the low runway gets a low ceiling
    # and one near the high runway a high floor (the source of the legitimate
    # cross-pad slope).  Also union the pad into edge-coupled clusters.
    node_band: dict = {}                # node_idx -> (lo, hi)
    node_refs: dict = {}                # cluster-root -> [ref...]  (debug)
    for s in layout.shapes:
        if s.role != ROLE_TERMINAL or s.polygon is None or s.polygon.is_empty:
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        idxs = [bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
                for (x, y) in ring]
        if sum(1 for i in idxs if i is not None) < 2:
            continue
        first = next(i for i in idxs if i is not None)
        for (x, y), idx in zip(ring, idxs):
            if idx is None:
                continue
            parent[_find(first)] = _find(idx)
            if idx in node_band:
                continue
            tkey, tgap = G.nearest_key(x, y)
            lo_n, hi_n = float("-inf"), float("inf")
            if tkey is not None:
                best: dict = {}         # ref -> (route, E_R)
                for dm, sg, eR, ref in rconn:
                    d = dm.get(tkey)
                    if d is None:
                        continue
                    route = d + sg + tgap
                    if ref not in best or route < best[ref][0]:
                        best[ref] = (route, eR)
                # §5.2 route-noise margin (s76): the route graph under-counts
                # real taxi paths ~4 %; a seed band used as a HARD level must
                # carry it or the floor over-pins the pad (HECA terminal7:
                # raw floor 71.5 from the 05C side vs the user-known ~70 —
                # the margin is exactly the difference).
                capm = cap * (1.0 + _ROUTE_NOISE_FRAC)
                for route, eR in best.values():
                    lo_n = max(lo_n, eR - capm * route)
                    hi_n = min(hi_n, eR + capm * route)
            node_band[idx] = (lo_n, hi_n)
        # RING-LIPSCHITZ smoothing of the per-node seed bands (s76 part 3):
        # adjacent ring vertices can enter the route graph at DIFFERENT
        # nodes and carry sharply different bands — the same graph-entry
        # discontinuity class the enforce's bands needed
        # (_lipschitz_tighten_bands).  Squeezed pads are now FROZEN at
        # levels derived from these bands, so a discontinuity becomes a
        # visible KINK inside the pad (HECA terminal2: 1.9 m over 5.8 m,
        # terminal1: 0.21 m at adjacent vertices — the in-sim "verify"
        # spikes).  Tighten each bound along the ring at the pad's own
        # grade cap so levels/midpoints are taken from a smooth field.
        ring_idx = [(i, xy) for xy, i in zip(ring, idxs) if i is not None]
        m_r = len(ring_idx)
        if m_r >= 3:
            for _cyc in range(3):
                changed = False
                for k in range(m_r):
                    i, (x1, y1) = ring_idx[k]
                    j, (x0, y0) = ring_idx[(k - 1) % m_r]
                    dseg = math.hypot(x1 - x0, y1 - y0)
                    lj, hj = node_band[j]
                    li, hi_v = node_band[i]
                    nh = min(hi_v, hj + cap * dseg)
                    nl = max(li, lj - cap * dseg)
                    if nh < hi_v - 1e-9 or nl > li + 1e-9:
                        node_band[i] = (nl, nh)
                        changed = True
                if not changed:
                    break
        node_refs.setdefault(_find(first), []).append(s.ref or "?")
    n_seeded = 0
    _dbg = _os.environ.get("O4_SEED_DEBUG") == "1"
    # Record every seeded level: the seed IS the route-feasible level for
    # the pad, and the standing ruling (terminals must NOT rise;
    # terminal7 ≈ 70) makes it the pad's CEILING for the rest of the solve
    # — the enforce's yield-down clamp reads this (s76: the relief lifted
    # squeezed pads 1.5-3 m above their seeds toward high aprons).
    seed_ceiling: dict = {}
    layout._terminal_seed_ceiling = seed_ceiling  # type: ignore[attr-defined]

    def _apply(i, val):
        elev[i] = val
        dem_elev[i] = val               # the vertex TARGET (DEM is only a guess;
        #                                 the pad goes where grade feasibility puts it)
        seed_ceiling[i] = val

    # Coupled clusters with their COMBINED band (intersection of member-node
    # bands).  A cluster is FLAT (one level) when that band is feasible — flatness
    # is preferred; it SLOPES (per-node) only when the combined band is infeasible
    # (the pad genuinely cannot be in grade to both a low and a high runway as one
    # level) AND terminals are allowed to grade.  This keeps a terminal on flat
    # terrain perfectly flat (no per-node seed noise) while letting a squeezed pad
    # like HECA 6/7/10 slope the minimum to honour grade (user 2026-06-09:
    # flatness yields to grade, not the other way round).
    clusters: dict = {}                 # root -> {nodes, lo, hi}
    for i, (lo_n, hi_n) in node_band.items():
        r = _find(i)
        c = clusters.setdefault(r, {"nodes": set(), "lo": float("-inf"),
                                    "hi": float("inf")})
        c["nodes"].add(i)
        c["lo"] = max(c["lo"], lo_n)
        c["hi"] = min(c["hi"], hi_n)
    sloped_nodes: set = set()           # terminals that must grade, not stay flat
    for r, c in clusters.items():
        lo, hi = c["lo"], c["hi"]
        if lo == float("-inf"):
            continue                    # not route-reachable from any runway
        nodes_c = c["nodes"]
        if lo <= hi:
            # FLAT: a single level (closest-to-DEM in the band) — flatness kept.
            dem = sum(dem_elev[i] for i in nodes_c) / len(nodes_c)
            seed = min(max(dem, lo), hi)
            for i in nodes_c:
                _apply(i, seed)
            tag = f"flat {seed:.1f}"
        else:
            # SQUEEZED (band infeasible: the pad straddles a low and a high runway
            # and CANNOT be one level in grade to both).  Flatness yields to grade
            # (user 2026-06-09): each vertex seeds at its own band so the pad
            # SLOPES the minimum, and the pad is marked GRADEABLE so the solver
            # keeps that slope instead of re-flattening it.
            for i in nodes_c:
                lo_n, hi_n = node_band[i]
                if lo_n == float("-inf"):
                    continue
                _apply(i, min(max(dem_elev[i], lo_n), hi_n) if lo_n <= hi_n
                       else 0.5 * (lo_n + hi_n))
            sloped_nodes |= nodes_c
            tag = "SLOPED (squeezed)"
        if _dbg:
            print(f"[termseed] {sorted(set(node_refs.get(r, [])))} "
                  f"n={len(nodes_c)} band[{lo:.1f},{hi:.1f}] -> {tag}")
        n_seeded += 1
    # Tell _build_shape_constraints which terminal vertices must GRADE (a squeezed
    # pad) rather than stay a rigid flat pad.
    layout._sloped_terminal_nodes = sloped_nodes  # type: ignore[attr-defined]
    return n_seeded


def _node_tiers(layout, bucket_to_idx, n):
    """Return ``[tier]`` per node — the OWNER tier used by the priority
    cascade.  A node's owner is the highest-priority role among the shapes
    that use it: ``_TIER_TAXI`` (sloping rects + junctions) >
    ``_TIER_APRON`` > ``_TIER_TERMINAL``.  Runway / runway-crossing nodes
    are HARD (seeded immutable) and keep tier 0 — they are never a phase's
    soft set.  Nodes used only by non-pavement shapes also stay 0.
    """
    tiers = [0] * n
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
            continue
        if s.role in _TAXI_TIER_ROLES:
            t = _TIER_TAXI
        elif s.role == ROLE_APRON:
            t = _TIER_APRON
        elif s.role == ROLE_TERMINAL:
            t = _TIER_TERMINAL
        else:
            continue
        try:
            coords = _open_ring(list(s.polygon.exterior.coords))
        except _GEOM_EXC:
            continue
        for x, y in coords:
            k = layout.canonical_points.get_or_add(float(x), float(y))
            idx = bucket_to_idx.get(k)
            if idx is not None and t > tiers[idx]:
                tiers[idx] = t
    return tiers


def _terminal_free_apron_nodes(layout, bucket_to_idx, tiers, n):
    """Return ``[bool]`` per node: True when the node belongs ONLY to
    apron(s) that DON'T touch a terminal.  Such aprons have no flat
    terminal floor pinning them, so they may YIELD (prong #2, user
    2026-05-23): in the relief phase they go soft alongside the taxi
    network and the cap projection lets them drop to relieve a
    connecting taxiway's over-grade — instead of being frozen by the
    cascade and forcing the taxiway over grade (CYXY apron #45 vs E)."""
    term_nodes = {i for i in range(n) if tiers[i] == _TIER_TERMINAL}
    node_is_apron = [False] * n
    node_pinned = [False] * n          # used by a terminal-touching apron
    for s in layout.shapes:
        if (s.role != ROLE_APRON or s.polygon is None
                or s.polygon.is_empty):
            continue
        try:
            coords = _open_ring(list(s.polygon.exterior.coords))
        except _GEOM_EXC:
            continue
        idxs = []
        for x, y in coords:
            k = layout.canonical_points.get_or_add(float(x), float(y))
            idx = bucket_to_idx.get(k)
            if idx is not None:
                idxs.append(idx)
        touches_term = any(i in term_nodes for i in idxs)
        for i in idxs:
            node_is_apron[i] = True
            if touches_term:
                node_pinned[i] = True
    return [node_is_apron[i] and tiers[i] == _TIER_APRON
            and not node_pinned[i] for i in range(n)]


def _runway_nodes(layout, bucket_to_idx, n):
    """Return ``[bool]`` per node: belongs to a runway / runway-crossing
    shape.  Used by the relief phase to let the runway yield a BOUNDED
    amount (prong #1, user 2026-05-23) so it shares the grade relief
    instead of an apron absorbing it all."""
    out = [False] * n
    for s in layout.shapes:
        if (s.role not in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING)
                or s.polygon is None or s.polygon.is_empty):
            continue
        try:
            coords = _open_ring(list(s.polygon.exterior.coords))
        except _GEOM_EXC:
            continue
        for x, y in coords:
            k = layout.canonical_points.get_or_add(float(x), float(y))
            idx = bucket_to_idx.get(k)
            if idx is not None:
                out[idx] = True
    return out


# ── Stage 2: seed initial elevations + HARD anchor flags ─────────


def _seed_elevations(layout, nodes, bucket_to_idx,
                     dem=None, tile_lat: int = 0, tile_lon: int = 0):
    """Returns ``(elev, is_hard, have_initial)``.

    HARD: only CIFP runway corners.  All other nodes are SOFT — even
    terminals and aprons, per user 2026-05-03 ("only the runway ends
    are immutable truth").

    Soft node seeding priority (highest first):
      1. Existing layout altitude_high/low/altitude/node_altitudes
         (warm-start from a previous solver pass).
      2. Per-vertex DEM sample at the node's (x, y).
      3. Nearest-HARD elevation (cheap geometric backfill).

    The DEM step is what lets a soft node settle at its natural
    terrain elevation when the rest of the graph allows it; cap
    projection in subsequent iterations pulls it down toward HARD
    anchors only where the per-edge grade cap is exceeded.
    """
    from auto_patch.elevation import _sample_dem
    n = len(nodes)
    elev: list[float] = [0.0] * n
    is_hard: list[bool] = [False] * n
    have_initial: list[bool] = [False] * n

    # Runway corners — HARD-anchor every runway segment, sloped or
    # flat.  The runway's elevation profile is authoritative truth
    # for adjacent pavement: when a junction shares a vertex with a
    # runway corner, that vertex must adopt the runway's elevation
    # so cap projection can pull the rest of the junction (and its
    # downstream chain of stubs / aprons) up toward it.
    #
    # Sloped segments are 4-corner rects with altitude_high/low.
    # Flat segments use a single ``altitude=`` tag and may carry an
    # arbitrary number of corners — junctions touching the edge
    # interior get inserted as new shared vertices upstream so the
    # solver gets denser HARD anchors along long flat runs (blast
    # pads, runway-interior flats).
    # Two-pass runway HARD seeding: process non-regraded (CIFP only)
    # shapes first, then regraded shapes (those with node_altitudes
    # from the seam pipeline) — the second pass OVERRIDES any shared
    # corner the first pass set.  This ensures that when a runway is
    # segmented into sub-rects and only the seam-crossing sub-rect
    # was regraded, the regraded values propagate to its shared
    # threshold corners with adjacent sub-rects.
    for pass_node_alts in (False, True):
        for s in layout.shapes:
            # ROLE_RUNWAY_CROSSING shares the runway HARD-anchor
            # path: its ``node_altitudes`` come from runway-segment
            # interpolation in ``_resolve_runway_crossings`` and
            # are authoritative; the solver must not reshape them.
            if s.role not in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING):
                continue
            if s.polygon is None or s.polygon.is_empty:
                continue
            has_node_alts = bool(s.node_altitudes)
            if has_node_alts != pass_node_alts:
                continue
            coords = _open_ring(list(s.polygon.exterior.coords))
            if len(coords) < 3:
                continue
            if s.altitude_high is not None and s.altitude_low is not None:
                if len(coords) != 4:
                    continue
                per = [s.altitude_high, s.altitude_low,
                       s.altitude_low, s.altitude_high]
            elif s.altitude is not None:
                per = [float(s.altitude)] * len(coords)
            elif s.node_altitudes:
                per = [float(a) for a in s.node_altitudes[:len(coords)]]
                if len(per) < len(coords):
                    per += [per[-1]] * (len(coords) - len(per))
            else:
                continue
            for (x, y), a in zip(coords, per):
                k = layout.canonical_points.get_or_add(float(x), float(y))
                idx = bucket_to_idx.get(k)
                if idx is None:
                    continue
                # Pass 1 (CIFP): only set if not already HARD.
                # Pass 2 (regraded): always override.
                if pass_node_alts or not is_hard[idx]:
                    elev[idx] = float(a)
                    is_hard[idx] = True
                    have_initial[idx] = True

    # Per user 2026-05-13: seam vertices are HARD anchors with
    # OVERRIDE priority over runway CIFP corners.  When a runway
    # interior vertex is on a tile-boundary seam, its DEM altitude
    # (already written into node_altitudes by apply_seam_dem_anchors)
    # wins over the CIFP-interpolated value at the same position.
    # Architecturally: seam wins because terrain mesh at the tile
    # boundary is pinned to raw HGT by Ortho4XP's preserve_boundary,
    # and we need pavement to match terrain there to avoid a visible
    # cliff in X-Plane.
    seam_keys = getattr(layout, "_seam_anchor_keys", None) or set()
    if seam_keys:
        for s in layout.shapes:
            if s.polygon is None or s.polygon.is_empty:
                continue
            if not s.node_altitudes:
                continue
            coords = _open_ring(list(s.polygon.exterior.coords))
            if len(coords) < 3:
                continue
            alts = list(s.node_altitudes[:len(coords)])
            for (x, y), a in zip(coords, alts):
                # Match the bucket convention used by seam_anchors.
                from ..layout import SHARED_VERTEX_TOL_M
                bk_s = 1.0 / SHARED_VERTEX_TOL_M
                seam_bk = (int(round(x * bk_s)), int(round(y * bk_s)))
                if seam_bk not in seam_keys:
                    continue
                k = layout.canonical_points.get_or_add(float(x), float(y))
                idx = bucket_to_idx.get(k)
                if idx is None:
                    continue
                # Seam wins: override any existing HARD value too.
                elev[idx] = float(a)
                is_hard[idx] = True
                have_initial[idx] = True

    # Warm-start soft nodes.
    for s in layout.shapes:
        if s.role not in PAVEMENT_ROLES or s.role == ROLE_RUNWAY:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        coords = _open_ring(list(s.polygon.exterior.coords))
        if (s.altitude_high is not None and s.altitude_low is not None
                and len(coords) == 4):
            per = [s.altitude_high, s.altitude_low,
                   s.altitude_low, s.altitude_high]
        elif s.altitude is not None:
            per = [float(s.altitude)] * len(coords)
        elif s.node_altitudes:
            per = [float(a) for a in s.node_altitudes[:len(coords)]]
            if len(per) < len(coords):
                per += [per[-1]] * (len(coords) - len(per))
        else:
            continue
        for (x, y), a in zip(coords, per):
            k = layout.canonical_points.get_or_add(float(x), float(y))
            idx = bucket_to_idx.get(k)
            if idx is None or is_hard[idx] or have_initial[idx]:
                continue
            elev[idx] = float(a)
            have_initial[idx] = True

    # DEM seed for soft nodes that warm-start didn't cover.
    if dem is not None and any(not h for h in have_initial):
        for i in range(n):
            if have_initial[i]:
                continue
            x, y = nodes[i]
            lat, lon = layout.m_to_ll(x, y)
            e = _sample_dem(dem, tile_lat, tile_lon, lat, lon)
            if e is not None:
                elev[i] = float(e)
                have_initial[i] = True

    # Backfill any node still without an initial value via nearest
    # HARD anchor's elevation (cheap geometric pass).
    if any(not h for h in have_initial):
        hard_pts = [(nodes[i][0], nodes[i][1], elev[i])
                    for i in range(n) if is_hard[i]]
        for i in range(n):
            if have_initial[i]:
                continue
            x, y = nodes[i]
            best_d2 = float("inf")
            best_e = 0.0
            for hx, hy, he in hard_pts:
                d2 = (hx - x) ** 2 + (hy - y) ** 2
                if d2 < best_d2:
                    best_d2 = d2
                    best_e = he
            elev[i] = best_e
            have_initial[i] = True

    return elev, is_hard, have_initial


def _sample_node_dem(layout, nodes, dem, tile_lat, tile_lon):
    """Return ``[dem_elev | None]`` per node — the terrain elevation
    each node is fit toward (closest-to-DEM within grade) by the
    hop-priority forward pass + directional relief.  None entries
    (no DEM, off-tile) are simply not attracted."""
    out: list[float | None] = [None] * len(nodes)
    if dem is None:
        return out
    from auto_patch.elevation import _sample_dem
    for i, (x, y) in enumerate(nodes):
        try:
            lat, lon = layout.m_to_ll(x, y)
            e = _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except _GEOM_EXC:
            e = None
        if e is not None:
            out[i] = float(e)
    return out


# ── Stage 3: edge construction (the per-axis rule lives here) ─────


JUNCTION_AXIS_PERP_TOL_M = 15.0  # taxi half-width + small slack


def _collect_junction_axes(layout, polygon):
    """Return every centerline / runway long-axis that passes
    through ``polygon`` — used by ``_build_edges`` to apply
    per-axis grade constraints to a junction.

    Sources:
    * ``layout.apt_taxi_centerlines`` — full apt.dat taxi network.
    * Each runway segment's long-axis (midpoints of its two short
      edges), for runway-crossing junctions.
    """
    from shapely.geometry import LineString
    axes = []
    apt_lines = getattr(layout, "apt_taxi_centerlines", None) or []
    for item in apt_lines:
        ln = item[0] if isinstance(item, tuple) else item
        if ln is None or ln.is_empty:
            continue
        try:
            if polygon.intersects(ln):
                axes.append(ln)
        except _GEOM_EXC:
            continue
    for s2 in layout.shapes:
        if s2.role != ROLE_RUNWAY:
            continue
        if s2.polygon is None or s2.polygon.is_empty:
            continue
        try:
            if not polygon.intersects(s2.polygon):
                continue
            rc = list(s2.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if rc and rc[0] == rc[-1]:
            rc = rc[:-1]
        if len(rc) != 4:
            continue
        a_mid = (0.5 * (rc[0][0] + rc[3][0]),
                 0.5 * (rc[0][1] + rc[3][1]))
        b_mid = (0.5 * (rc[1][0] + rc[2][0]),
                 0.5 * (rc[1][1] + rc[2][1]))
        try:
            axes.append(LineString([a_mid, b_mid]))
        except _GEOM_EXC:
            continue
    return axes


def _build_edges(layout, bucket_to_idx, roles=None, add_runway_anchor=True
                  ) -> tuple[dict[tuple[int, int], float],
                             dict[tuple[int, int], float]]:
    """Build the unified graph's edge list with role-aware geometry.

    ``roles`` (cascade): when given, only shapes whose role is in the set
    contribute edges — so each cascade phase builds just its own tier's
    edges (lower tiers couple to frozen higher tiers via shared HARD nodes,
    not cross-tier edges).  ``add_runway_anchor`` gates the taxi→runway
    anchor edges (only the taxi phase wants them).

    For RECT roles (taxi rects, runway segments): ring edges only.
    The within-rect constraint is axial; cross-section flatness
    groups handle the perpendicular dimension.

    For JUNCTION: ring edges + per-axis grade edges.  For each
    apt.dat taxi centerline or runway long-axis that passes
    through the polygon, the vertices within
    ``JUNCTION_AXIS_PERP_TOL_M`` perpendicular of the axis form
    a group; edges between group members use the ALONG-AXIS
    projected distance as the edge length.  Vertices not near any
    axis are bound only by ring continuity.  Per user 2026-05-18:
    a junction may slope in multiple directions along its
    converging centerlines and 1.5 % is enforced ALONG each axis,
    NOT cross-axially.

    For APRON: ring edges + all-pair Euclidean over the body, but
    pairs lying along a crossing apt.dat taxilane axis use the looser
    along-axis arc length (directional relief along the lane) when the
    per-axis model is active.  The body / stand area keeps the
    all-direction cap (a free-maneuvering surface).  For TERMINAL: ring
    edges + all-pair Euclidean (every-direction cap, no axes).

    Per-edge cap = role's max grade × edge length.  When two
    shapes contribute to the same vertex pair, the tighter cap
    wins.
    """
    from shapely.geometry import Point
    edge_grade: dict[tuple[int, int], float] = {}
    edge_length: dict[tuple[int, int], float] = {}

    def _add_edge(ui, uj, length, gr):
        if ui is None or uj is None or ui == uj:
            return
        if length < 0.1:
            return
        key = (ui, uj) if ui < uj else (uj, ui)
        cur_g = edge_grade.get(key, float("inf"))
        if gr < cur_g:
            edge_grade[key] = gr
        cur_l = edge_length.get(key, length)
        edge_length[key] = min(cur_l, length)

    for s in layout.shapes:
        if s.role not in PAVEMENT_ROLES:
            continue
        if roles is not None and s.role not in roles:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        coords = _open_ring(list(s.polygon.exterior.coords))
        if len(coords) < 2:
            continue
        gr = _role_grade(s.role)
        m = len(coords)
        node_idx = [bucket_to_idx.get(layout.canonical_points.get_or_add(float(x), float(y)))
                    for x, y in coords]
        # Ring edges (every shape).
        for i in range(m):
            j = (i + 1) % m
            x1, y1 = coords[i]
            x2, y2 = coords[j]
            length = math.hypot(x2 - x1, y2 - y1)
            _add_edge(node_idx[i], node_idx[j], length, gr)
        # Rects / runways / runway-crossings: ring-only, no spatial
        # pairs.  Runway-crossings are HARD-anchored via the
        # runway-interpolated ``node_altitudes`` seed; spatial
        # edges would constrain them needlessly.
        if (s.role in SLOPING_RECT_ROLES
                or s.role in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING)):
            continue
        # Junction / apron / terminal: all-pair within the polygon.
        # Per user 2026-05-18: "a junction should not exceed 1.5 %
        # across ANY portion, not just along its edge" → all-pair
        # Euclidean cap.
        #
        # Hybrid centerline budget (user 2026-05-22): grade along a
        # taxi centerline is cumulative ALONG the centerline path, not
        # the straight-line chord — a junction can legitimately descend
        # a curved taxi route faster (per chord metre) than 1.5 % while
        # staying ≤ 1.5 % per metre TRAVELLED.  For a JUNCTION pair that
        # both lie within ``JUNCTION_AXIS_PERP_TOL_M`` of a common
        # centerline, use the longer ALONG-CENTERLINE arc distance as
        # the edge length (looser cap in the taxi direction) so the
        # junction can grade down the route toward its stubs.  Cross-
        # axis pairs (no shared centerline) keep the Euclidean chord →
        # the all-pair cliff guard still holds off-route.  Aprons /
        # terminals are true multi-directional surfaces and keep pure
        # Euclidean.
        # Junctions always collect their converging axes (arc-lengthening
        # + diagonal-drop below).  Aprons collect taxilane axes only when
        # the per-axis model is active (session 47): along-lane pairs get
        # the looser arc length so an apron can grade along a taxilane,
        # while the apron BODY (no shared lane) keeps its all-pair cap.
        if s.role == ROLE_JUNCTION:
            axes = _collect_junction_axes(layout, s.polygon)
        elif s.role == ROLE_APRON and _PER_AXIS_JUNCTIONS:
            axes = _collect_junction_axes(layout, s.polygon)
        else:
            axes = []
        for i in range(m):
            xi, yi = coords[i]
            for j in range(i + 2, m):
                if i == 0 and j == m - 1:
                    continue  # ring-wrap pair already added
                xj, yj = coords[j]
                length = math.hypot(xj - xi, yj - yi)
                along_axis = False
                if axes:
                    pi = Point(xi, yi)
                    pj = Point(xj, yj)
                    for ax in axes:
                        if (ax.distance(pi) <= JUNCTION_AXIS_PERP_TOL_M
                                and ax.distance(pj)
                                <= JUNCTION_AXIS_PERP_TOL_M):
                            arc = abs(ax.project(pi) - ax.project(pj))
                            if arc > length:
                                length = arc
                            along_axis = True
                # Per-axis junctions (user 2026-05-22): the inter-centerline
                # DIAGONAL is an unregulated direction (ICAO Annex 14 §3.9 /
                # EASA CS-ADR-DSN.D.265/.280 regulate LONGITUDINAL along the
                # route + TRANSVERSE, not the diagonal).  Drop cross-axis
                # junction pairs so the all-pair Euclidean cap stops forbidding
                # a junction that legitimately slopes along converging routes
                # over real terrain (SPLP -10025).  Ring + along-centerline
                # pairs still constrain it.  Aprons/terminals (axes==[]) are
                # true multi-directional surfaces — keep their all-pair cap.
                # Drop the unregulated cross-axis diagonal for JUNCTIONS
                # only.  Aprons keep every non-lane pair as an all-pair
                # Euclidean edge — the general apron body / stand area is a
                # free-maneuvering surface that must stay flat-capped (the
                # stricter stand cap arrives with the ramp-start zones in a
                # later phase); only its along-lane pairs were loosened above.
                if (_PER_AXIS_JUNCTIONS and s.role == ROLE_JUNCTION
                        and axes and not along_axis):
                    continue
                _add_edge(node_idx[i], node_idx[j], length, gr)

    # ── Taxi → runway anchor (user 2026-05-22, revives the dormant
    # TAXI_ANCHOR_DIST_M).  A taxi connector approaching a runway must
    # grade FROM the runway elevation down its centerline path, not
    # float at terrain.  Connect each near-runway taxi/junction vertex
    # to the nearest runway CORNER node within TAXI_ANCHOR_DIST_M so cap
    # projection propagates the runway HARD anchor along the connector
    # (HECA stub T4 sat 2.75 m below runway 05C/23C with no anchor).
    if not add_runway_anchor:
        return edge_grade, edge_length
    from auto_patch.elevation import TAXI_ANCHOR_DIST_M
    rwy_corners: list[tuple[int, float, float]] = []
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        for x, y in _open_ring(list(s.polygon.exterior.coords)):
            k = layout.canonical_points.get_or_add(float(x), float(y))
            ri = bucket_to_idx.get(k)
            if ri is not None:
                rwy_corners.append((ri, float(x), float(y)))
    if rwy_corners:
        anchor2 = TAXI_ANCHOR_DIST_M * TAXI_ANCHOR_DIST_M
        for s in layout.shapes:
            if s.role not in SLOPING_RECT_ROLES and s.role != ROLE_JUNCTION:
                continue
            if s.polygon is None or s.polygon.is_empty:
                continue
            for x, y in _open_ring(list(s.polygon.exterior.coords)):
                k = layout.canonical_points.get_or_add(float(x), float(y))
                vi = bucket_to_idx.get(k)
                if vi is None:
                    continue
                best_idx = None
                best_d2 = anchor2
                for ri, rx, ry in rwy_corners:
                    if ri == vi:
                        best_idx = None  # vertex IS a runway corner
                        break
                    d2 = (rx - x) * (rx - x) + (ry - y) * (ry - y)
                    if d2 < best_d2:
                        best_d2 = d2
                        best_idx = ri
                if best_idx is not None:
                    _add_edge(vi, best_idx, math.sqrt(best_d2),
                              TAXI_MAX_GRADE)

    return edge_grade, edge_length


# ── Stage 6: write elevations back to layout shapes ──────────────


def _writeback(layout, elev, bucket_to_idx):
    """Apply solved elevations to layout shapes.

    For taxi rects: ensure the polygon's vertex order is canonical
    (corners 0, 3 at the higher axis-end; corners 1, 2 at the
    lower).  The OSM emit interpolates altitude_high/low across
    polygon corners via the legacy convention ``[high, low, low,
    high]`` for indices 0..3 — that mapping is wrong for any rect
    whose polygon happens to be ring-rotated relative to canonical,
    leading to a phantom perpendicular slope (the source of the
    user 2026-05-03 SPJC F-stub report).  Rotating the ring at
    writeback aligns the convention with the actual axis-end
    geometry.
    """
    from shapely.geometry import Polygon
    from auto_patch.elevation import (
        _corner_elevation_bucket, _short_end_pairs_by_axis,
    )
    n_terms = n_rects = n_juncs = 0
    for s in layout.shapes:
        if s.role not in PAVEMENT_ROLES:
            continue
        # Runway shapes are normally skipped (their altitudes come
        # from CIFP — HARD-anchored, immutable through the solver).
        # Exceptions where the writeback DOES run:
        #   * Seam-converted runway sub-rects (user 2026-05-13): they
        #     have ``node_altitudes`` set; we write per-vertex
        #     solver-output altitudes so shared corners with adjacent
        #     sub-rects agree on the regraded value.
        #   * Non-4-corner runway shapes (user 2026-05-19): a runway
        #     segment that lost its canonical 4-corner form through
        #     downstream geometry passes (crossing union, snap-to-
        #     corner, etc.) is no longer a sloped rect — its
        #     altitude_high/low tags are stale because X-Plane's
        #     planar 4-corner convention requires exactly 4 corners.
        #     Convert to ``node_altitudes`` so the OSM emit + the
        #     no-vertex-on-sloping-edge invariant treat it as the
        #     non-rect it actually is.
        if s.role == ROLE_RUNWAY and not s.node_altitudes:
            _rc_check = list(s.polygon.exterior.coords) if s.polygon else []
            if _rc_check and _rc_check[0] == _rc_check[-1]:
                _rc_check = _rc_check[:-1]
            if len(_rc_check) == 4:
                # CIFP-plane piece: normally authoritative as-is — but a
                # runway-FLEX (dip/rise re-smooth) mutates ``elev`` at
                # runway nodes, and skipping the refresh leaves THIS
                # piece's plane at the pre-flex profile while its
                # node_altitudes neighbours move (HECA 05L: piece at
                # 60.1/60.4 sharing corners with a risen 62.8 ring =
                # a 2.4 m emitted cliff ON the runway).  Refresh the
                # plane from the solved corners when they moved.
                if NETWORK_PROFILE_MODEL:
                    _ce9 = _read_corner_elevs(
                        _rc_check, elev, bucket_to_idx, layout)
                    if (_ce9 is not None
                            and s.altitude_high is not None
                            and s.altitude_low is not None
                            and any(min(abs(c9 - s.altitude_high),
                                        abs(c9 - s.altitude_low)) > 0.05
                                    for c9 in _ce9)):
                        _nc9, _hi9, _lo9 = _canonicalise_rect(
                            _rc_check, _ce9, s.source_axis,
                            _short_end_pairs_by_axis)
                        if _nc9 is not None:
                            if _nc9 != _rc_check:
                                s.polygon = Polygon(_nc9 + [_nc9[0]])
                            s.altitude_high = round(float(_hi9), 1)
                            s.altitude_low = round(float(_lo9), 1)
                            s.altitude = None
                            n_rects += 1
                continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        ring_closed = coords and coords[0] == coords[-1]
        coords_open = coords[:-1] if ring_closed else coords
        corner_elevs = _read_corner_elevs(
            coords_open, elev, bucket_to_idx, layout)
        if corner_elevs is None:
            continue
        if s.role == ROLE_TERMINAL and _role_grade(ROLE_TERMINAL) <= 0.0:
            # Terminal is FLAT (the default: TERMINAL_MAX_GRADE = 0, a terminal
            # sits on one floor altitude — per user 2026-05-18).  The flat
            # equality group already enforced this in the solver; average is just
            # a defensive round.  When TERMINAL_MAX_GRADE > 0 the terminal grades
            # like an apron and falls through to the per-corner branch below.
            avg = sum(corner_elevs) / len(corner_elevs)
            s.altitude = round(float(avg), 1)
            s.altitude_high = None
            s.altitude_low = None
            s.node_altitudes = None
            n_terms += 1
        elif s.role in (ROLE_APRON, ROLE_TERMINAL):
            # Per user 2026-05-18: aprons are NOT 100 % flat — they
            # satisfy 1.5 % across their surface, NOT zero gradient.
            # Keep the solver's per-corner altitudes (which it
            # already constrained via all-pair Euclidean edges) so
            # adjacent aprons that share corners don't end up at
            # 4-8 m cliff steps (each apron previously averaged to
            # its own single altitude → adjacent aprons diverged).
            alts = [round(float(e), 1) for e in corner_elevs]
            if ring_closed:
                alts.append(alts[0])
            s.node_altitudes = alts
            s.altitude = None
            s.altitude_high = None
            s.altitude_low = None
            n_terms += 1
        elif s.role in SLOPING_RECT_ROLES:
            # Per user 2026-05-13: keep node_altitudes when the shape
            # came in with them — even for 4-corner shapes.  This
            # preserves per-vertex precision for runway sub-rects
            # adjacent to seam-affected sub-rects: their shared
            # corners receive HARD seam altitudes that aren't coplanar
            # with the other 2 CIFP corners, so altitude_high/low
            # (which assumes a planar surface) would average and
            # introduce a > 1 m step at the shared boundary.
            had_node_alts = s.node_altitudes is not None
            if (len(coords_open) == 4 and not had_node_alts
                    and _rect_short_ends_perpendicular(
                        coords_open, s.source_axis)):
                new_coords, hi, lo = _canonicalise_rect(
                    coords_open, corner_elevs, s.source_axis,
                    _short_end_pairs_by_axis)
                if new_coords is None:
                    continue
                if new_coords != coords_open:
                    s.polygon = Polygon(new_coords + [new_coords[0]])
                s.altitude_high = round(float(hi), 1)
                s.altitude_low = round(float(lo), 1)
                s.altitude = None
                s.node_altitudes = None
                n_rects += 1
            else:
                alts = [round(float(e), 1) for e in corner_elevs]
                if ring_closed:
                    alts.append(alts[0])
                s.node_altitudes = alts
                s.altitude_high = None
                s.altitude_low = None
                s.altitude = None
                n_rects += 1
        elif s.role in (ROLE_JUNCTION, ROLE_SERVICE_JUNCTION):
            # Junction + service-road-network junction: per-corner
            # node_altitudes (all-pair shapes, irregular polygons).
            alts = [round(float(e), 1) for e in corner_elevs]
            if ring_closed:
                alts.append(alts[0])
            s.node_altitudes = alts
            s.altitude = None
            n_juncs += 1
        elif s.role == ROLE_RUNWAY:
            # Seam-converted runway sub-rect — write per-vertex
            # altitudes (the only runway shapes that reach here have
            # node_altitudes pre-set; the skip-guard above filters
            # the CIFP-only altitude_high/low ones).
            alts = [round(float(e), 1) for e in corner_elevs]
            if ring_closed:
                alts.append(alts[0])
            s.node_altitudes = alts
            s.altitude = None
            s.altitude_high = None
            s.altitude_low = None
            n_rects += 1
    return n_terms, n_rects, n_juncs


def _read_corner_elevs(coords_open, elev, bucket_to_idx, layout=None):
    out = []
    for x, y in coords_open:
        idx = bucket_to_idx.get(layout.canonical_points.get_or_add(float(x), float(y)))
        if idx is None:
            return None
        out.append(elev[idx])
    return out


_RECT_SHORT_END_MAX_AXIS_DOT = 0.7  # |edge·axis|/|edge| above this = the
                                     # "short end" is really axis-parallel
                                     # (degenerate non-rect quad, e.g. a
                                     # tapering wedge from junction-splitting)


def _rect_short_ends_perpendicular(coords_open, source_axis) -> bool:
    """True when a 4-corner ring is a genuine sloping rect: its two
    axis-end (short) edges — as paired by ``_short_end_pairs_by_axis`` —
    are roughly PERPENDICULAR to ``source_axis``.

    Projection-based pairing breaks on distorted quads (opposite sides
    not parallel): it can group two corners whose connecting edge runs
    ALONG the axis, so collapsing to ``altitude_high``/``altitude_low``
    produces a surface that slopes ACROSS a perpendicular edge.  Such a
    shape is not a canonical rect and must stay ``node_altitudes`` (user
    2026-05-24).  A clean rect's short ends have |edge·axis| ≈ 0.
    """
    from auto_patch.elevation import _short_end_pairs_by_axis
    if source_axis is None or source_axis.is_empty:
        return False
    ax = list(source_axis.coords)
    if len(ax) < 2:
        return False
    axdx, axdy = ax[-1][0] - ax[0][0], ax[-1][1] - ax[0][1]
    axlen = math.hypot(axdx, axdy)
    if axlen < 1e-6:
        return False
    aux, auy = axdx / axlen, axdy / axlen
    sp, ep = _short_end_pairs_by_axis(coords_open, source_axis)
    if sp is None:
        return False
    for pair in (sp, ep):
        ax0, ay0 = coords_open[pair[0]]
        ax1, ay1 = coords_open[pair[1]]
        ex, ey = ax1 - ax0, ay1 - ay0
        elen = math.hypot(ex, ey)
        if elen < 1e-6:
            return False
        if abs(ex * aux + ey * auy) / elen > _RECT_SHORT_END_MAX_AXIS_DOT:
            return False
    return True


def _canonicalise_rect(coords_open, corner_elevs, source_axis,
                        short_end_pairs_fn):
    """Rotate a rect's 4-vertex ring (and its corner elevations)
    so corners 0, 3 are at the higher axis-end and 1, 2 at the
    lower.  Returns ``(new_coords, hi, lo)`` or ``(None, ...)`` if
    rotation can't be determined.
    """
    sp, ep = short_end_pairs_fn(coords_open, source_axis)
    if sp is None:
        sp, ep = (0, 3), (1, 2)
    a_avg = (corner_elevs[sp[0]] + corner_elevs[sp[1]]) / 2.0
    b_avg = (corner_elevs[ep[0]] + corner_elevs[ep[1]]) / 2.0
    high_pair = sp if a_avg >= b_avg else ep
    hi, lo = max(a_avg, b_avg), min(a_avg, b_avg)
    # Rotation that makes high_pair == (0, 3).
    rotation = _rotation_for_high_pair(high_pair)
    if rotation == 0:
        return list(coords_open), hi, lo
    new_coords = [coords_open[(i - rotation) % 4]
                  for i in range(4)]
    return new_coords, hi, lo


def _rotation_for_high_pair(high_pair) -> int:
    """Return the right-shift k such that rotating the 4-vertex
    ring by k positions makes ``high_pair`` map to ``(0, 3)``.

    Mapping: under right-shift k, old index ``i`` becomes new
    index ``(i + k) % 4``.  We solve for k so that
    ``{(high_pair[0] + k) % 4, (high_pair[1] + k) % 4} == {0, 3}``.
    """
    target = {0, 3}
    a, b = high_pair
    for k in range(4):
        if {(a + k) % 4, (b + k) % 4} == target:
            return k
    return 0


def _report(icao, iters_used, max_iters, elapsed,
             n_terms, n_rects, n_juncs):
    import O4_UI_Utils as UI
    UI.vprint(1,
        f"  [pav-builder] {icao}: per-surface Jacobi solver "
        f"converged in {iters_used}/{max_iters} iters "
        f"({elapsed:.2f} s); applied to {n_terms} terminal/apron(s), "
        f"{n_rects} rect(s), {n_juncs} junction(s).")
