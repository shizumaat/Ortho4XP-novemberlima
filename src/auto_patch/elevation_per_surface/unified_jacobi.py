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
    ROLE_GRADE_LIMITS, RUNWAY_END_FRACTION, RUNWAY_END_GRADE,
    RUNWAY_MAX_GRADE, TERMINAL_PADS_SLOPE)
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
_PER_AXIS_JUNCTIONS = False

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
    if not all(relief_hard):
        relief_eg = dict(taxi_eg)
        relief_eg.update(apron_eg)
        relief_eg.update(term_eg)
        relief_el = dict(taxi_el)
        relief_el.update(apron_el)
        relief_el.update(term_el)
        shape_constraints = _build_shape_constraints(
            layout, bucket_to_idx)

        _step_dbg = _os.environ.get("O4_STEP_DEBUG") == "1"
        if _step_dbg:
            v, w = _count_within_viol(elev, shape_constraints)
            print(f"[step] {icao} after STEP1 forward cascade: "
                  f"within-edge viol={v} worst={w:.2f}m")
        total_iters += _directional_relief(
            n, elev, relief_hard, relief_eg, relief_el,
            shape_constraints, _RELIEF_MAX_ITERS, tol_m)
        if _step_dbg:
            v, w = _count_within_viol(elev, shape_constraints)
            print(f"[step] {icao} after STEP2 reverse relief+yield: "
                  f"within-edge viol={v} worst={w:.2f}m")
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
        # FINAL within-shape enforcement (difference-constraint solve): drive
        # every FEASIBLE within-shape edge to <=cap against the now-settled
        # runway/seam anchors, and report the band-pinned residual (the only
        # part a within-shape solve cannot fix — it needs an anchor to flex).
        if _step_dbg:
            v, w = _count_within_viol(elev, shape_constraints)
            print(f"[step] {icao} after STEP3 runway flex: "
                  f"within-edge viol={v} worst={w:.2f}m")
        _enforce_within_shape_grade(
            elev, shape_constraints, base_hard,
            nodes=nodes, layout=layout, bucket_to_idx=bucket_to_idx, icao=icao)
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
        # KNOWN RESIDUAL (s73, with junction visibility): a junction EDGE
        # that GRAZES a sloping rect's long edge over a run without shared
        # vertices (HECA junction -10193's 314 m edge converges onto rect
        # TX29's 144 m edge, lateral 0.49→0.01 m) cannot follow the rect's
        # plane once the junction slopes — its straight lerp deviates up to
        # ~0.7 m at the touch point (2 mid-edge steps).  An altitude-only
        # vertex snap measured ZERO applicable vertices (the deviation
        # peaks mid-edge, where no vertex exists) — the fix is PRE-SOLVE
        # GEOMETRY: conform the grazing junction edge to the rect's corner
        # projections (the coincident-run-collapse class, s68
        # ``_near_edge_line``), so the solver couples them via shared
        # nodes.  Tracked in STATUS.

    n_terms, n_rects, n_juncs = _writeback(
        layout, elev, bucket_to_idx)
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


def _runway_reach_bands(nodes, elev, runway_nodes, seam_nodes, all_edges, cap,
                        layout):
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
    """
    from auto_patch.taxi_routing import build_taxi_route_graph
    n = len(nodes)
    NEG, POS = float("-inf"), float("inf")
    lo = [NEG] * n
    hi = [POS] * n
    G = build_taxi_route_graph(layout)
    if G.coord and runway_nodes:
        # Cache each node's nearest centerline (key, gap) — one O(|coord|) scan
        # per node, reused for seeds and queries.
        near: dict[int, tuple] = {}

        def _near(i):
            r = near.get(i)
            if r is None:
                r = G.nearest_key(*nodes[i])
                near[i] = r
            return r

        def _propagate(sign):
            # Multi-source Dijkstra over the centerline graph (edge weight =
            # cap*length) seeded at each runway anchor's centerline entry.
            dist: dict = {}
            pq: list = []
            for a in runway_nodes:
                if a >= n:
                    continue
                key, gap = _near(a)
                if key is None:
                    continue
                v0 = sign * elev[a] + cap * gap
                if v0 < dist.get(key, POS):
                    dist[key] = v0
                    heapq.heappush(pq, (v0, key))
            while pq:
                d, u = heapq.heappop(pq)
                if d > dist.get(u, POS):
                    continue
                for v, w in G.adj.get(u, ()):  # type: ignore[union-attr]
                    nd = d + cap * w
                    if nd < dist.get(v, POS):
                        dist[v] = nd
                        heapq.heappush(pq, (nd, v))
            return dist
        ceil_key = _propagate(1.0)            # elev[a] + cap*(gap+route)
        floor_key = _propagate(-1.0)          # -elev[a] + cap*(gap+route)
        for v in range(n):
            key, gap = _near(v)
            if key is None:
                continue
            c = ceil_key.get(key)
            if c is not None:
                hi[v] = c + cap * gap
            f = floor_key.get(key)
            if f is not None:
                lo[v] = -(f + cap * gap)
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
                                icao=None) -> int:
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
        lo, hi = _runway_reach_bands(
            nodes, elev, runway_nodes, seam_nodes, all_edges,
            TAXI_MAX_GRADE, layout)
    else:
        lo, hi = _grade_bands(n, elev, is_hard, all_edges)
    band_pinned = {i for i in range(n) if lo[i] > hi[i] + 1e-6}
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
    _dbg = _os.environ.get("O4_ENFORCE_DEBUG") == "1"
    if _dbg:
        v0 = sum(1 for (i, j, c) in all_edges
                 if c > 0 and abs(elev[i] - elev[j]) > c + 1e-4)
    sweeps, resid = _project_within_bands(
        elev, all_edges, is_hard, lo, hi, coupling,
        held_extra=set(),
        max_sweeps=_WITHIN_ENFORCE_MAX_SWEEPS,
        tol=_SPREAD_COMPLY_TOL_M)
    if _dbg:
        v1 = sum(1 for (i, j, c) in all_edges
                 if c > 0 and abs(elev[i] - elev[j]) > c + 1e-4)
        print(f"[enforce] {icao}: {sweeps} sweeps, edge-viol {v0}->{v1}, "
              f"residual {resid:.3f} m, band-pinned = {len(band_pinned)}")
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


def _visible_grade_edges(coords, idx, cap, polygon, container=None):
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
    true void (grass between arms) stays excluded."""
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
            if _vis is not None:
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
            edges.extend(_visible_grade_edges(
                coords, idx, cap, s.polygon,
                container=(airside_buf if s.role == ROLE_JUNCTION
                           else None)))
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


# Route-distance measurement uncertainty, as a fraction of the route length.
# The taxi-route graph under-counts real taxi paths: endpoint stubs are
# straight chords (the centerline rows stop short of runway edges) and row
# joins are uncurved corners (no fillets).  Measured at HECA's A4↔T4 corridor
# (s73): graph 3,217 m vs the ≥3,353 m reality requires — ~4 % short.  A route
# demand below ``frac · cap · route_d`` is within measurement noise of a
# feasible corridor; the demand synthesis drops it instead of flexing a runway.
_ROUTE_NOISE_FRAC = 0.04


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
    from auto_patch.taxi_routing import build_taxi_route_graph
    cps = layout.canonical_points
    G = build_taxi_route_graph(layout)
    if not getattr(G, "coord", None):
        return {}
    # AUGMENT the graph with RUNWAY CENTERLINES (2026-06-09): the apt.dat
    # taxi-route rows stop at/near the runway edge, so threshold anchors
    # were unreachable and the legitimate other-runway demand (e.g. HECA
    # 05L 60.7 + 1.5 %·~3.2 km ≈ 108.5 at the T4 join) never formed.
    # Each runway piece contributes its cross-end midpoint pair as an
    # edge (the piece's centerline segment — pieces share cross-ends, so
    # the chain connects); each midpoint also bridges to the nearest
    # PRE-EXISTING taxi node within 40 m (the taxi rows' on-runway
    # endpoints, e.g. HECA node 181 "05R/23L_start").  Local to the flex
    # — the terminal seed keeps the unaugmented graph.
    _taxi_nodes_snapshot = list(G.coord.values())

    def _aug_edge(pa, pb):
        ka, kb = G._key(*pa), G._key(*pb)
        G.coord.setdefault(ka, pa)
        G.coord.setdefault(kb, pb)
        if ka == kb:
            return
        w = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        G.adj.setdefault(ka, []).append((kb, w))
        G.adj.setdefault(kb, []).append((ka, w))

    _mids: list = []
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY or s.polygon is None or s.polygon.is_empty:
            continue
        if flex_ref is not None and (s.ref or "") == flex_ref:
            continue        # no rise-corridor along the flexing runway
        ring = _open_ring(list(s.polygon.exterior.coords))
        if len(ring) != 4:
            continue
        edges4 = [(ring[k], ring[(k + 1) % 4]) for k in range(4)]
        edges4.sort(key=lambda ab: math.hypot(
            ab[1][0] - ab[0][0], ab[1][1] - ab[0][1]))
        m0 = ((edges4[0][0][0] + edges4[0][1][0]) / 2.0,
              (edges4[0][0][1] + edges4[0][1][1]) / 2.0)
        m1 = ((edges4[1][0][0] + edges4[1][1][0]) / 2.0,
              (edges4[1][0][1] + edges4[1][1][1]) / 2.0)
        _aug_edge(m0, m1)
        _mids.append(m0)
        _mids.append(m1)
    for mp in _mids:
        best_pt = None
        best_d = 40.0
        for (tx, ty) in _taxi_nodes_snapshot:
            d = math.hypot(tx - mp[0], ty - mp[1])
            if d < best_d:
                best_d = d
                best_pt = (tx, ty)
        if best_pt is not None:
            _aug_edge(mp, best_pt)
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
    from auto_patch.taxi_routing import build_taxi_route_graph
    cps = layout.canonical_points
    G = build_taxi_route_graph(layout)
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
                for route, eR in best.values():
                    lo_n = max(lo_n, eR - cap * route)
                    hi_n = min(hi_n, eR + cap * route)
            node_band[idx] = (lo_n, hi_n)
        node_refs.setdefault(_find(first), []).append(s.ref or "?")
    n_seeded = 0
    _dbg = _os.environ.get("O4_SEED_DEBUG") == "1"

    def _apply(i, val):
        elev[i] = val
        dem_elev[i] = val               # the vertex TARGET (DEM is only a guess;
        #                                 the pad goes where grade feasibility puts it)

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
