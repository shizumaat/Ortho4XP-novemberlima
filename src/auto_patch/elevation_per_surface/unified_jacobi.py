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
import time as _time
from collections import deque

from shapely.errors import GEOSException, TopologicalError

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

# Dykstra L2 projection iteration cap (per cascade phase).  A uniform
# over-grade slope is corrected by anchor information propagating inward one
# node per sweep, so convergence scales with the longest anchor-free run —
# generous here, but each sweep is cheap and convergence stops early via tol.
_L2_MAX_ITERS = 2000

# Per-cascade-phase grade-fit selector.  ``False`` (default) = the proven
# DEM-attraction + cap-projection relaxation (holds flat pavement on terrain
# via the asymmetric floor; non-regressing).  ``True`` = the L2-closest-to-DEM
# Dykstra projection (``_l2_compliant_fit``) — the architecturally-correct
# "follow DEM clamped to grade" fit, but only useful once junction grading is
# fully PER-AXIS (the all-pair Euclidean cap otherwise drives genuinely-steep
# junctions infeasible and the L2 fit smears the residual onto short stubs).
# Flip to True together with the per-axis solver/audit work.
_USE_L2_FIT = True

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
    """Per-role max grade cap.  Taxiway-family + runway + junction share
    ``TAXI_MAX_GRADE`` (1.5 %); ground-vehicle service roads get the
    looser 4 % (cars handle steeper terrain); apron / terminal use
    ``APRON_MAX_GRADE`` (1.5 %).  Checked before the sloping-rect branch
    because service_road is itself a sloping rect but must NOT inherit 1.5 %.
    """
    if role in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION):
        return SERVICE_ROAD_MAX_GRADE
    if role in (ROLE_RUNWAY, *SLOPING_RECT_ROLES, ROLE_JUNCTION):
        return TAXI_MAX_GRADE
    return APRON_MAX_GRADE


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

    rect_flat_groups = _build_rect_cross_section_groups(
        layout, bucket_to_idx)
    terminal_groups = _build_terminal_groups(
        layout, bucket_to_idx)

    total_iters = 0

    def _eq_pairs_from_groups(groups):
        """Flatten flatness groups into equality (cap-0) constraint pairs:
        a group ``[a, b, c, …]`` becomes the chain ``(a,b),(b,c),…`` which
        forces all members equal."""
        pairs = []
        for grp in groups:
            for k in range(len(grp) - 1):
                if grp[k] != grp[k + 1]:
                    pairs.append((grp[k], grp[k + 1]))
        return pairs

    def _run_phase(phase_tier, eg, el, eq_pairs, rect_grps, term_grps):
        """Solve one cascade tier: freeze all other tiers (+ base HARD), then
        fit a grade-compliant surface for this tier's soft nodes against the
        frozen anchors.  Two fits are available (see ``_USE_L2_FIT``): the
        proven DEM-attraction relaxation, or the L2-closest-to-DEM Dykstra
        projection."""
        nonlocal total_iters
        if not eg and not eq_pairs:
            return
        is_hard_p = [base_hard[i] or tiers[i] != phase_tier
                     for i in range(n)]
        if all(is_hard_p):
            return
        if _USE_L2_FIT:
            total_iters += _compliant_spread_fit(
                n, elev, is_hard_p, dem_elev, eg, el, eq_pairs,
                _L2_MAX_ITERS, tol_m)
        else:
            adj_p = _build_adjacency(n, eg, el)
            total_iters += _run_jacobi(
                elev, is_hard_p, adj_p, list(eg.keys()),
                eg, el, term_grps, rect_grps, max_iters, tol_m,
                dem_elev=dem_elev, use_attraction=True)

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
    if _USE_L2_FIT:
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
            total_iters += _directional_relief(
                n, elev, relief_hard, relief_eg, relief_el,
                shape_constraints, _RELIEF_MAX_ITERS, tol_m)
            # STEP 3 (user 2026-05-28): an impossible apron<->runway connection
            # (a junction/stub wedged between soft apron and a DEM-dipped
            # runway-anchored node) has nowhere to go while every runway node
            # is HARD.  Free the runway INTERIOR (CIFP thresholds stay hard)
            # and re-solve the bands so the dip rises and the gap spreads over
            # the runway's length.  No-op unless a residual violation remains.
            total_iters += _relax_runway_and_resolve(
                n, elev, layout, bucket_to_idx, base_hard,
                shape_constraints, tol_m)

    n_terms, n_rects, n_juncs = _writeback(
        layout, elev, bucket_to_idx)
    _report(icao, total_iters, max_iters,
             _time.time() - t_start,
             n_terms, n_rects, n_juncs)


_SPREAD_OMEGA = 1.0          # cap-projection relaxation (>1 SOR diverges here)
_SPREAD_COMPLY_TOL_M = 0.02  # iterate until every edge is within this of cap
# Relief iteration budget — umbrella ceiling for the within-bands convergence.
_RELIEF_MAX_ITERS = 12000


def _compliant_spread_fit(n, elev, is_hard, dem_elev, edge_grade, edge_length,
                          eq_pairs, max_iters, tol_m, node_bounds=None,
                          reseed: bool = True, stiffness=None) -> int:
    """Spread soft nodes to a grade-compliant surface near the DEM, by
    over-relaxed cap projection iterated to FULL grade compliance.

    The DEM is a PREFERENCE, not a constraint (user 2026-05-22): soft nodes are
    seeded at terrain, then every over-grade edge is projected toward its cap
    — where the terrain already complies the nodes stay on it, where it is too
    steep the excess is split and PROPAGATES into the neighbouring network
    until no edge exceeds grade (the descent dips below / rises above terrain
    as needed and smooths out).  Equality (flatness) pairs are cap-0 edges.

    Why over-relaxed + compliance-stopped: plain cyclic cap projection on a
    long over-grade run propagates the anchors' influence one node per sweep
    (O(chain^2) — Dykstra was correct but never finished, leaving stub/A
    unsmoothed).  SOR (omega>1) collapses that to ~O(chain), and stopping on
    the actual max violation (not the per-iter change) guarantees the output is
    fully compliant.  Mutates ``elev`` in place; returns iterations used.

    ``reseed`` (user 2026-05-24): when False, soft nodes keep their CURRENT
    elevation as the start — used by the relief "bounce" so it adjusts the
    MINIMUM amount from the cascade's outward solve, rather than discarding it
    by resetting to DEM (which made terminal-free aprons absorb the whole
    relief and sink ~12 m below terrain).
    """
    if reseed:
        for i in range(n):
            if not is_hard[i] and dem_elev[i] is not None:
                # Bounded nodes keep their current value as the center (e.g. a
                # runway-yield node centered on its CIFP profile); only
                # unbounded soft nodes re-seed at terrain.
                if node_bounds is None or node_bounds[i] is None:
                    elev[i] = float(dem_elev[i])
    ineq = [(u, v, edge_length[(u, v)] * gr)
            for (u, v), gr in edge_grade.items()]
    eqs = [(a, b) for (a, b) in eq_pairs]
    if not ineq and not eqs:
        return 0
    comply = max(tol_m, _SPREAD_COMPLY_TOL_M)
    w = _SPREAD_OMEGA
    for it in range(max_iters):
        # Flatness equality (exact projection — no over-relax).
        for a, b in eqs:
            ha, hb = is_hard[a], is_hard[b]
            if ha and hb:
                continue
            if ha:
                elev[b] = elev[a]
            elif hb:
                elev[a] = elev[b]
            else:
                m = 0.5 * (elev[a] + elev[b])
                elev[a] = m
                elev[b] = m
        # Grade inequality (over-relaxed for soft-soft; exact toward a HARD).
        max_viol = 0.0
        for u, v, cap in ineq:
            d = elev[u] - elev[v]
            excess = abs(d) - cap
            if excess <= 0.0:
                continue
            if excess > max_viol:
                max_viol = excess
            hu, hv = is_hard[u], is_hard[v]
            if hu and hv:
                continue
            s = 1.0 if d > 0 else -1.0
            if hu:
                elev[v] += s * excess
            elif hv:
                elev[u] -= s * excess
            else:
                # Distribute the correction by STIFFNESS: the stiffer node
                # moves less (user 2026-05-24).  A terminal is a stiff soft
                # anchor — it holds its DEM-centroid and yields only the
                # MINIMUM when no compliant path exists, so the flexible apron/
                # taxi side absorbs the grade and the terminal barely moves.
                # Default (no stiffness) = symmetric 50/50.
                if stiffness is not None:
                    ku, kv = stiffness[u], stiffness[v]
                    fu = kv / (ku + kv)   # u's share — large when v is stiffer
                else:
                    fu = 0.5
                elev[u] -= s * w * excess * fu
                elev[v] += s * w * excess * (1.0 - fu)
        # Clamp bounded soft nodes to their per-node deviation window so
        # no single surface absorbs the whole relief (user 2026-05-23:
        # spread the descent across runway-yield + apron-yield).  A node
        # held at a bound leaves a residual violation on its edges, which
        # propagates to the other soft nodes — exactly the spreading.
        if node_bounds is not None:
            for i in range(n):
                b = node_bounds[i]
                if b is not None:
                    if elev[i] < b[0]:
                        elev[i] = b[0]
                    elif elev[i] > b[1]:
                        elev[i] = b[1]
        if max_viol < comply:
            return it + 1
    return max_iters


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
    ineq = [((u, v), edge_length[(u, v)] * gr)
            for (u, v), gr in edge_grade.items()]
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
    # Forward-pass (DEM) level each terminal cluster is anchored to — the
    # minimum-shift reference; Phase 1 already made each member flat at its DEM
    # centroid.  AREA-WEIGHT the cluster level so the dominant (largest) pad
    # anchors it (else a small many-vertex pad would skew a node-count mean).
    term_level0: list[float] = []
    for _members in _clusters.values():
        _nodes: set = set()
        for _ti in _members:
            _nodes |= set(term_scs[_ti]["nodes"])
        terminal_groups.append(_nodes)
        _tot_a = sum(term_scs[_ti]["area"] for _ti in _members) or 1.0
        _lvl = sum(
            (sum(elev[i] for i in term_scs[_ti]["nodes"])
             / len(term_scs[_ti]["nodes"])) * term_scs[_ti]["area"]
            for _ti in _members) / _tot_a
        term_level0.append(_lvl)
    terminal_nodes: set = set().union(*terminal_groups) if terminal_groups \
        else set()
    # Per-edge grade-cap adjacency (node -> [(neighbour, cap_m)]) to find the
    # feasible band for a terminal's rigid level from its settled NON-terminal
    # neighbours (an apron's all-pair edges out of the shared vertices).
    cap_adj: dict[int, list[tuple[int, float]]] = {}
    for (u, v), gr in edge_grade.items():
        c = edge_length[(u, v)] * gr
        cap_adj.setdefault(u, []).append((v, c))
        cap_adj.setdefault(v, []).append((u, c))
    group_of_node: dict[int, int] = {}
    for gi, g in enumerate(terminal_groups):
        for i in g:
            group_of_node[i] = gi

    def _rigid_shift_terminal(gi: int) -> None:
        """Translate terminal group ``gi`` as one flat unit to the level
        closest to its forward-pass DEM level that keeps every connection to a
        settled non-terminal neighbour within grade (minimum shift)."""
        g = terminal_groups[gi]
        lo, hi = float("-inf"), float("inf")
        for i in g:
            for (j, c) in cap_adj.get(i, ()):  # type: ignore[arg-type]
                if j in terminal_nodes:
                    continue          # within/between terminals: no constraint
                lo = max(lo, elev[j] - c)
                hi = min(hi, elev[j] + c)
        t0 = term_level0[gi]
        if lo <= hi:
            t = min(max(t0, lo), hi)   # already feasible -> stay; else min move
        else:
            t = 0.5 * (lo + hi)        # infeasible band: minimise worst violation
        for i in g:
            elev[i] = t

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
            _rigid_shift_terminal(gi)         # adjust the whole terminal (flat)
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
    n_sweeps, _viol = _project_within_bands(
        elev, all_edges, is_hard, lo, hi, coupling,
        held_extra=terminal_nodes,
        max_sweeps=1000,
        tol=max(tol_m, _SPREAD_COMPLY_TOL_M))
    return 1 + n_sweeps


def _build_shape_constraints(layout, bucket_to_idx):
    """Per-shape grade constraints for the directional relief: one entry per
    soft pavement shape with ``{nodes, edges, flat}`` — its node indices, its
    OWN internal grade edges ``(i, j, cap_m)``, and whether it must stay flat
    (terminal).  Rects use flat-cross (cap≈0) + axial edges; apron/junction
    use all-pair; terminal is flat.  Runway/seam are HARD, not included."""
    out = []
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
        flat = (s.role == ROLE_TERMINAL)
        cap = _role_grade(s.role)
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
        else:
            # All-pair (apron / junction / seam-cut rect).
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
                    edges.append((idx[a], idx[b], cap * el))
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


def _relax_runway_and_resolve(n, elev, layout, bucket_to_idx, base_hard,
                              shape_constraints, tol_m) -> int:
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

    snapshot0 = list(elev)
    sweeps_total = 0
    committed_soft: set | None = None
    for soft in levels:
        if not soft:
            continue
        is_hard2 = list(base_hard)
        for i in soft:
            is_hard2[i] = False
        elev[:] = snapshot0                       # each level restarts clean
        lo, hi = _grade_bands(n, elev, is_hard2, all_edges)
        sweeps, _viol = _project_within_bands(
            elev, all_edges, is_hard2, lo, hi, coupling,
            held_extra=terminal_nodes, max_sweeps=1000, tol=comply)
        sweeps_total += sweeps
        w1, c1, t1 = _within_excess_stats(elev, pav_edges, comply)
        # Accept iff it clears at least one violation (or shrinks total excess)
        # WITHOUT worsening the worst — so a runway/threshold yield that fixes
        # one connector can't be vetoed by an unrelated stubborn violation, and
        # can't trade a small violation for a bigger one.
        improved = (w1 <= w0 + comply
                    and (c1 < c0 or t1 < t0 - comply))
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
    used as the DEM-attraction target in ``_run_jacobi``.  None entries
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


def _build_adjacency(n, edge_grade, edge_length):
    adj: list[list[tuple[int, float, float]]] = [[] for _ in range(n)]
    for (u, v), gr in edge_grade.items():
        L = edge_length[(u, v)]
        adj[u].append((v, L, gr))
        adj[v].append((u, L, gr))
    return adj


# ── Stage 4: terminal flatness groups ────────────────────────────


def _build_rect_cross_section_groups(layout, bucket_to_idx):
    """Per user 2026-05-03: a taxi rect slopes along its
    ``source_axis`` only — never perpendicular to it.  The two
    corners at each axis-end (the rect's "axis-end edge", or what
    the legacy code called the "short end") share one elevation;
    the cross-section is exactly flat.

    Each rect contributes TWO flatness groups, one per axis-end.
    The solver runs ``_equalize_groups`` on these every iteration,
    same mechanic as terminal flatness.  ``altitude_high`` /
    ``altitude_low`` written by the solver thus correspond to one
    value per axis-end with no perpendicular component.

    Returns ``[[idx_a, idx_b], ...]`` — one group per axis-end (two
    groups per rect).
    """
    from auto_patch.elevation import (
        _corner_elevation_bucket, _short_end_pairs_by_axis,
    )
    groups: list[list[int]] = []
    for s in layout.shapes:
        if s.role not in SLOPING_RECT_ROLES:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        coords = _open_ring(list(s.polygon.exterior.coords))
        if len(coords) != 4:
            continue
        if s.source_axis is None or s.source_axis.is_empty:
            continue
        sp, ep = _short_end_pairs_by_axis(coords, s.source_axis)
        if sp is None:
            continue
        for pair in (sp, ep):
            idxs = []
            for i in pair:
                if 0 <= i < len(coords):
                    k = layout.canonical_points.get_or_add(float(coords[i][0]), float(coords[i][1]))
                    if k in bucket_to_idx:
                        idxs.append(bucket_to_idx[k])
            if len(idxs) >= 2 and idxs[0] != idxs[1]:
                groups.append(idxs)
    return groups


def _build_terminal_groups(layout, bucket_to_idx):
    """Each terminal contributes one group of node indices that
    must share a single elevation (the flatness constraint).
    """
    groups: list[list[int]] = []
    for s in layout.shapes:
        if s.role != ROLE_TERMINAL:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        coords = _open_ring(list(s.polygon.exterior.coords))
        idxs = []
        for x, y in coords:
            k = layout.canonical_points.get_or_add(float(x), float(y))
            if k in bucket_to_idx:
                idxs.append(bucket_to_idx[k])
        if len(idxs) >= 2:
            groups.append(idxs)
    return groups


# ── Stage 5: damped Jacobi + cap projection iteration ────────────


def _equalize_groups(elev, is_hard, groups):
    """Set every member of each group to the group's mean elevation.
    HARD members are immutable; if a HARD member exists, the group
    averages the SOFT members and pulls them toward the HARD value
    (cap projection in subsequent sweeps will pull the rest of the
    graph back into compliance).

    Used for both terminal flatness (one group per terminal, all
    corners) and rect cross-section flatness (one group per rect
    short-end, two corners each).
    """
    for grp in groups:
        if not grp:
            continue
        hard_in_grp = [i for i in grp if is_hard[i]]
        if hard_in_grp:
            target = elev[hard_in_grp[0]]
            for i in grp:
                if not is_hard[i]:
                    elev[i] = target
        else:
            avg = sum(elev[i] for i in grp) / len(grp)
            for i in grp:
                elev[i] = avg


def _run_jacobi(elev, is_hard, adj, edge_list, edge_grade,
                edge_length, terminal_groups,
                rect_flat_groups,
                max_iters, tol_m, dem_elev=None,
                use_attraction=True) -> int:
    """DEM-attraction + cap-projection relaxation (user 2026-05-03,
    DEM attraction added 2026-05-22).

    Earlier iterations of this solver included a damped-Jacobi
    NEIGHBOUR-average step before cap projection.  Jacobi pulls every
    soft node toward the weighted mean of its neighbours, which
    propagates HARD anchor values up through the graph and over-flattens
    DEM-seeded soft nodes (CYXY taxi E ended up at 700 m next to a 700 m
    runway, even though DEM said 715 m and the grade chain through stubs
    allowed reaching it).  That step was removed.

    But cap-projection-ONLY has the opposite failure: it only ever
    REDUCES violations, so a soft node that warm-started LOW (a stale
    value from a prior solver pass) is never lifted back toward its
    terrain — the node stays low whenever its long chain to a HARD anchor
    happens to be within-cap.  At HECA the taxiway/apron network
    (spanning the genuinely-low south terminals and the high runways)
    sank ~5-8 m below its own DEM, leaving a 7 m cliff where stub T4
    meets runway 05C/23C.

    Fix: each iteration pull every SOFT node a DECAYING fraction
    (``DEM_ATTRACTION × DEM_ATTRACTION_DECAY^it``) toward its DEM
    elevation, THEN cap-project.  A node below its DEM with no binding
    upper cap rises to terrain; a node whose DEM exceeds what the caps
    permit is held at the cap by projection.  The geometric decay means
    the attraction vanishes after a few hundred iterations, so the loop
    still converges to a fixed point (``tol_m``) under pure cap
    projection — no oscillation at cap-pinned nodes.  Net convergence is
    to "as close to DEM as the per-edge grade caps allow", which is the
    documented "reach the highs and lows in DEM that are possible within
    grade limits" rule.

    Two equality constraint groups run each iteration: terminals
    (all corners equal) and rect axis-end pairs (per user 2026-
    05-03: rects slope along source_axis only, axis-perpendicular
    is flat).
    """
    n = len(elev)
    use_dem = (use_attraction and dem_elev is not None
               and DEM_ATTRACTION > 0.0)
    for it in range(max_iters):
        prev_elev = list(elev)
        # 0) DEM attraction — pull soft nodes toward terrain by a
        # geometrically-decaying fraction (skips HARD nodes and nodes
        # with no DEM sample).
        if use_dem:
            decay = DEM_ATTRACTION_DECAY ** it
            for i in range(n):
                if is_hard[i]:
                    continue
                d = dem_elev[i]
                if d is None:
                    continue
                # Asymmetric: strong pull UP toward terrain (undo
                # spurious below-DEM drag), gentle pull DOWN.  Grade
                # wins via the cap-projection that runs next.
                w = DEM_FLOOR_ATTRACTION if elev[i] < d else DEM_ATTRACTION
                a = w * decay
                if a > DEM_ATTRACTION_MIN:
                    elev[i] += a * (d - elev[i])
        # 1) Multi-sweep edge grade-cap projection — only force
        # acting on soft nodes.  Each sweep visits every edge; an
        # edge is projected (excess split symmetrically for
        # soft-soft, asymmetrically toward the soft side for
        # soft-hard) only when it currently violates its cap.
        for _sweep in range(CAP_SWEEPS_PER_ITER):
            any_proj = False
            for (u, v) in edge_list:
                L = edge_length[(u, v)]
                gr = edge_grade[(u, v)]
                diff = elev[u] - elev[v]
                cap = L * gr
                if abs(diff) <= cap:
                    continue
                excess = abs(diff) - cap
                sign = 1 if diff > 0 else -1
                if is_hard[u] and is_hard[v]:
                    continue
                if is_hard[u]:
                    elev[v] += sign * excess
                    any_proj = True
                elif is_hard[v]:
                    elev[u] -= sign * excess
                    any_proj = True
                else:
                    half = 0.5 * excess * sign
                    elev[u] -= half
                    elev[v] += half
                    any_proj = True
            if not any_proj:
                break
        # 2) Equality constraints — terminal flatness and rect
        # axis-end (cross-section) flatness.
        _equalize_groups(elev, is_hard, terminal_groups)
        _equalize_groups(elev, is_hard, rect_flat_groups)
        # 3) Convergence.
        max_change = 0.0
        for i in range(n):
            if is_hard[i]:
                continue
            d = abs(prev_elev[i] - elev[i])
            if d > max_change:
                max_change = d
        if max_change < tol_m:
            return it + 1
    return max_iters


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
        if s.role == ROLE_TERMINAL:
            # Terminal is FLAT (per user 2026-05-18: a terminal sits
            # on one floor altitude).  The terminal-flatness equality
            # group already enforced this in the solver; average is
            # just a defensive round.
            avg = sum(corner_elevs) / len(corner_elevs)
            s.altitude = round(float(avg), 1)
            s.altitude_high = None
            s.altitude_low = None
            s.node_altitudes = None
            n_terms += 1
        elif s.role == ROLE_APRON:
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
