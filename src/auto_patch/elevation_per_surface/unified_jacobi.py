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

import math
import time as _time

from shapely.errors import GEOSException, TopologicalError

from auto_patch.elevation import APRON_MAX_GRADE, TAXI_MAX_GRADE
from auto_patch.layout import (
    ROLE_APRON, ROLE_BOUNDARY, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_RUNWAY, ROLE_RUNWAY_CROSSING,
    ROLE_SECONDARY_PARALLEL, ROLE_STUB, ROLE_TERMINAL,
)

# Narrow exception tuple for shapely / numeric-geometry failure
# modes.  Programming errors propagate so they surface immediately.
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)


SLOPING_RECT_ROLES = (
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR,
)

PAVEMENT_ROLES = {
    ROLE_RUNWAY, *SLOPING_RECT_ROLES,
    ROLE_APRON, ROLE_TERMINAL, ROLE_JUNCTION,
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

_TAXI_TIER_ROLES = frozenset((*SLOPING_RECT_ROLES, ROLE_JUNCTION))

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
# diagonals this allows.  Default False until that audit change lands together.
_PER_AXIS_JUNCTIONS = False

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
    """Per-role max grade cap.  All roles now share ``TAXI_MAX_GRADE``
    (1.5 %, user 2026-05-18): the apron-reclassification pipeline
    pass folds true apron-territory pavement into ``ROLE_APRON``,
    and the cap was relaxed from the FAA-1.0 % parking-surface limit
    to match the taxiway cap so reclassified shapes don't trip the
    solver / audit at every other vertex pair.
    """
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

    # INVERTED cascade (user 2026-05-23): terminal → apron → taxi.
    # Tier 3 — TERMINALS first: flat plane anchored at the DEM-mean of the
    # footprint (the flat-equality group averages the per-vertex DEM seed),
    # yielding only to any seam/runway HARD node it touches.  Because a
    # terminal↔apron boundary node is now terminal-owned, the terminal
    # flattens it here and the apron (next) inherits the flat floor —
    # whole-flat terminal + 1:1 aprons fall out natively (no post-flatten).
    _run_phase(_TIER_TERMINAL, term_eg, term_el,
               _eq_pairs_from_groups(terminal_groups),
               [], terminal_groups)
    # Tier 2 — APRONS grade outward from the frozen terminal, following the
    # DEM up to max grade.
    _run_phase(_TIER_APRON, apron_eg, apron_el, [], [], [])
    # Tier 1 — TAXI network grades from the frozen aprons to the runway /
    # seam HARD anchors (rect cross-sections stay flat).
    _run_phase(_TIER_TAXI, taxi_eg, taxi_el,
               _eq_pairs_from_groups(rect_flat_groups),
               rect_flat_groups, [])

    # Relief phase (prong #2, user 2026-05-23) — terminal-free aprons
    # YIELD with the taxi network.  The cascade froze every apron before
    # the taxi solved, so a taxiway bridging a low runway and a high
    # terminal-free apron (CYXY E -> apron #45) is forced over grade with
    # no recourse.  Here the terminal-free apron nodes go SOFT alongside
    # the taxi nodes (terminals, terminal-anchored aprons, runway/seam
    # stay HARD), and the cap projection lets the apron drop only as far
    # as relieving the over-grade requires.  The DEM seed keeps it near
    # terrain; it descends below only where grade demands (and the edge-
    # blur caution applies — it never chases edge DEM, only yields to grade).
    if _USE_L2_FIT:
        yieldable = _terminal_free_apron_nodes(layout, bucket_to_idx, tiers, n)
        if any(yieldable):
            # Terminal-free aprons go soft with the taxi network; the
            # runway stays HARD (its yield is a LAST RESORT, not here —
            # user 2026-05-23).  The yield is grade-driven and UNBOUNDED:
            # the DEM is unreliable at excavated terraces (CYXY apron #45's
            # DEM reads ~714 m but the real terrace is 705 m), so the cap
            # projection — not a DEM-relative bound — finds the right
            # level.  It drops only as far as relieving the over-grade
            # requires, which lands at the true terrace.
            relief_hard = [base_hard[i] or not (
                tiers[i] == _TIER_TAXI or yieldable[i]) for i in range(n)]
            if not all(relief_hard):
                relief_eg = dict(taxi_eg)
                relief_eg.update(apron_eg)
                relief_el = dict(taxi_el)
                relief_el.update(apron_el)
                total_iters += _compliant_spread_fit(
                    n, elev, relief_hard, dem_elev, relief_eg, relief_el,
                    _eq_pairs_from_groups(rect_flat_groups),
                    _L2_MAX_ITERS, tol_m)

    n_terms, n_rects, n_juncs = _writeback(
        layout, elev, bucket_to_idx)
    _report(icao, total_iters, max_iters,
             _time.time() - t_start,
             n_terms, n_rects, n_juncs)


_SPREAD_OMEGA = 1.0          # cap-projection relaxation (>1 SOR diverges here)
_SPREAD_COMPLY_TOL_M = 0.02  # iterate until every edge is within this of cap


def _compliant_spread_fit(n, elev, is_hard, dem_elev, edge_grade, edge_length,
                          eq_pairs, max_iters, tol_m, node_bounds=None) -> int:
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
    """
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
                move = w * 0.5 * excess
                elev[u] -= s * move
                elev[v] += s * move
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

    For APRON / TERMINAL: ring edges + all-pair Euclidean spatial
    edges.  Aprons must satisfy 1.5 % across the entire interior
    surface (every-direction cap).

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
        axes = (_collect_junction_axes(layout, s.polygon)
                if s.role == ROLE_JUNCTION else [])
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
                if _PER_AXIS_JUNCTIONS and axes and not along_axis:
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
            if len(coords_open) == 4 and not had_node_alts:
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
        elif s.role == ROLE_JUNCTION:
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
