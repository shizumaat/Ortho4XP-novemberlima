"""Elevation-neutral solver PRIMITIVES (extracted from the former
unified_jacobi cascade, M2 cleanup 2026-06-27).  Node list, DEM seed/
sample, within-shape constraint + level-coupling graph, runway node/edge
sets, writeback, report.  The legacy multi-pass solve() cascade was
deleted; route_profile.solve_route_profile is the only solver.
See docs/cleanup_consolidation_plan.md (M2).
"""
from __future__ import annotations

import heapq
import math
import os as _os
import time as _time
from collections import deque

from shapely.errors import GEOSException, TopologicalError

from auto_patch.config import (
    APRON_BACK_EDGE_GRADE, APRON_BACK_EDGE_RAMPS,
    W2_CLEAN_BANDS,
    APRON_CORRIDOR_GEODESIC, APRON_CORRIDOR_SEED_RADIUS_M,
    APRON_CORRIDOR_SMOOTH_GRADE, APRON_CORRIDOR_SMOOTH_RADIUS_M,
    NETWORK_PROFILE_MODEL, ROLE_GRADE_LIMITS, ROUTE_FIELD_LOCAL_WINDOW_M,
    ROUTE_FIELD_MODEL, RUNWAY_END_FRACTION,
    SEAM_FIELD_ANCHORS, SPREAD_APRON_GRADE, SEAM_APRON_COMPLEX_POLISH,
    RUNWAY_END_GRADE, RUNWAY_MAX_GRADE, SURFACE_FAIRING,
    SURFACE_FAIRING_MAX_MOVE_M, TAXI_CORRIDOR_PROFILE,
    TAXI_SLACK_TERMINALS,
    TAXIWAY_MAX_GRADE_CHANGE_PER_M, TERMINAL_LEAF_LEVELS,
    TERMINAL_CHORD_MAX_GRADE, TERMINAL_CHORD_REACH_M,
    TERMINAL_NATURAL_LEVELS,
    TERMINAL_PADS_SLOPE, WRITE_ARBITRATION)
from auto_patch.elevation import (
    APRON_MAX_GRADE, SERVICE_ROAD_MAX_GRADE, TAXI_MAX_GRADE)
from auto_patch.config import (
    taxi_grade_cap_for_letter, TAXI_MAX_GRADE_NARROW, JUNCTION_NARROW_GRADE,
    CORRIDOR_SPINE_CHAINS,
    FLATNESS_CERTIFICATE_RATE_FACTOR, FLAT_CERTIFICATE_COVERAGE,
    RECT_CROSS_FLATNESS_TOLERANCE_M)
from auto_patch.layout import (
    ROLE_APRON, ROLE_BOUNDARY, ROLE_BRIDGE_CAUSEWAY, ROLE_BRIDGE_TRENCH,
    ROLE_CROSS_CONNECTOR, ROLE_GRADED_STRIP, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_RUNWAY, ROLE_RUNWAY_CLEARANCE,
    ROLE_RUNWAY_CROSSING,
    ROLE_SECONDARY_PARALLEL, ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION,
    ROLE_STUB, ROLE_BUILDING, taxi_shape_code_letter,
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

# Taxi rects that FOLLOW THE SPINE PROFILE (the aircraft taxiways) — service
# roads are excluded (not taxi spines; their own role/grade).

PAVEMENT_ROLES = {
    ROLE_RUNWAY, *SLOPING_RECT_ROLES,
    ROLE_APRON, ROLE_BUILDING, ROLE_JUNCTION,
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
    # Object-bridge terrain plates (feature B, user directive round 8):
    # FIRST-CLASS graph members — their ring vertices enter the
    # canonical node registry and every one is a HARD PIN at the grade
    # law value (``layout._object_bridge_pin_values``, written at shape
    # birth), the RUNWAY_CROSSING pattern: the solver grades the
    # neighbouring pavement to meet them and never reshapes them.
    # Gate off ⇒ no such shapes exist and membership is vacuous.
    ROLE_BRIDGE_TRENCH,
    ROLE_BRIDGE_CAUSEWAY,
}


# ── Terrain-role admission scaffolding (Slice B Stage B0) ─────────
# docs/slice_b_solver_absorption_design.md.  The absorption moves the three
# post-solve terrain emitters PRE-SOLVE so their ring vertices become
# first-class solver variables the way the object-bridge plate roles above
# already are: admitted to the canonical node registry and the solver node
# list, then (later stages) given constraint builders.  This is the
# ADMISSION scaffolding ONLY — the per-role constraint builders are stages
# B1-B3 and do NOT exist yet.
#
# The three absorption families, each a (layout role, provenance ref) pair:
#   * runway-end skirt  → (ROLE_RUNWAY_CLEARANCE, "runway_end_skirt")     [B1]
#   * gap-fill spine     → (ROLE_GRADED_STRIP,     "gap_fill_spine")      [B2]
#   * graded strip band  → (ROLE_GRADED_STRIP,     "adjacent_ground")     [B3]
# ROLE-ONLY ADMISSION IS AMBIGUOUS (Slice B stage B3 order 1 correction):
# gap-fill faces/spines and adjacent-ground bands BOTH carry ROLE_GRADED_STRIP,
# yet they are different families with different admission paths (gap-fill via
# the dedicated pre-solve spine store; adjacent-ground via ring vertices) and
# different sub-gates.  Flipping ROLE_GRADED_STRIP admission wholesale would
# grab both.  Admission therefore keys on the (role, ref) PAIR, not the role.
TERRAIN_GRAPH_REFS = frozenset({
    (ROLE_RUNWAY_CLEARANCE, "runway_end_skirt"),
    (ROLE_GRADED_STRIP, "gap_fill_spine"),
    (ROLE_GRADED_STRIP, "adjacent_ground"),
})
# Back-compat ROLE projection (call sites that legitimately want role
# granularity — the _writeback skip, the skirt-pin gate).  Disjoint from
# PAVEMENT_ROLES (asserted in tests) so admitting a family never double-counts
# today's pavement.
TERRAIN_GRAPH_ROLES = frozenset(role for role, _ref in TERRAIN_GRAPH_REFS)


def admitted_terrain_refs():
    """The set of ``(role, ref)`` TERRAIN GRAPH FAMILIES whose vertices are
    admitted to the canonical node registry and the solver node list this
    build (Slice B stage B0 scaffolding, refined to (role, ref) granularity at
    stage B3 order 1).

    Gated by ``config.ONE_SOLVE_TERRAIN`` (master, default OFF) and the three
    per-family sub-gates.  Returns a possibly-empty ``frozenset`` of
    ``(role, ref)`` pairs.  EMPTY whenever the master gate is off (the default)
    OR every sub-gate is off — and admission of an empty family set is a
    structural no-op, so the node list, the constraint graph and the solve are
    byte-identical to today.  Config is read at CALL TIME (tests toggle the
    gates via env + module reload / monkeypatch).

    NOTE (B3 order 1): the ``ONE_SOLVE_TERRAIN_GRADED_STRIP`` sub-gate below is
    the B0 ADMISSION gate for the adjacent-ground band family (B3 order 2,
    still OFF); it is deliberately SEPARATE from
    ``ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT`` (the order-1 pre-solve
    footprint construction gate), which admits NOTHING here — construction
    moves pre-solve while values stay analytic post-solve."""
    from auto_patch import config as _cfg
    if not getattr(_cfg, "ONE_SOLVE_TERRAIN", False):
        return frozenset()
    admitted: set = set()
    if getattr(_cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", False):
        admitted.add((ROLE_RUNWAY_CLEARANCE, "runway_end_skirt"))
    if getattr(_cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", False):
        admitted.add((ROLE_GRADED_STRIP, "gap_fill_spine"))
    if getattr(_cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", False):
        # HARD DEPENDENCY CHAIN (Slice B stage B3 order 2, coordinator
        # ruling): band variable admission builds on (a) the pre-solve
        # footprint construction (the zone-node grid lives on the
        # construct store), (b) the B1 pre-solve skirts (band footprints
        # probe the skirt rings in the pre-solve static block), and
        # (c) the B2 gap-spine admission (the interval-aware reach
        # envelope and the writeback split are shared machinery, and the
        # acceptance is only defined on the full stack).  A partial gate
        # set is a misconfiguration that would silently measure the
        # wrong thing — fail LOUDLY instead.
        _missing = [name for name, on in (
            ("O4_ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT",
             getattr(_cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT",
                     False)),
            ("O4_ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT",
             getattr(_cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", False)),
            ("O4_ONE_SOLVE_TERRAIN_GAP_FILL_SPINE",
             getattr(_cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", False)),
        ) if not on]
        if _missing:
            raise RuntimeError(
                "O4_ONE_SOLVE_TERRAIN_GRADED_STRIP=1 (adjacent-ground "
                "band variable admission, Slice B stage B3 order 2) "
                "requires ALL of its dependency gates ON; missing: "
                + ", ".join(_missing))
        admitted.add((ROLE_GRADED_STRIP, "adjacent_ground"))
    return frozenset(admitted)


def admitted_terrain_roles():
    """Back-compat ROLE projection of ``admitted_terrain_refs`` — the set of
    layout roles with at least one admitted ``(role, ref)`` family this build.
    Used by call sites that gate at role granularity and then filter by ref
    themselves (the skirt-pin block, the ``_writeback`` skip)."""
    return frozenset(role for role, _ref in admitted_terrain_refs())


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
# Seam-level threshold-yield: the band solve tilts the runway through the hard
# seam, which can leave a residual vertical-curve kink at the seam crossing
# (eliminating it needs the seam's runway node anchored in the FAA smooth — a
# follow-up).  Allow this many marginal kinks so a grade-compliant seam yield
# still commits instead of leaving the runway grade-violating.

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


def _shape_grade(layout, s) -> float:
    """Per-SHAPE max grade cap.  Identical to :func:`_role_grade` for every
    role EXCEPT the sized taxiway-family rects: when the
    ``TAXI_GRADE_BY_WIDTH`` gate is on, a narrow taxiway (ICAO code A/B,
    width < 15 m) earns the steeper ``TAXI_MAX_GRADE_NARROW`` (3 %) cap
    instead of the uniform 1.5 % — ICAO Annex 14 §3.9.3.  The width class
    comes from :func:`taxi_shape_code_letter` (apt.dat letter, else measured
    rect width); gate off / non-taxiway roles fall straight back to the
    role cap, so the solver stays byte-identical to the uniform baseline.

    APRON-EDGE ADOPTION (USER RULING 2026-07-06): a service road /
    service junction sharing an edge with an apron follows the APRON
    grading rules — the flag is set by the pipeline's adoption pass and
    overrides the role cap."""
    if getattr(s, "adopts_apron_grade", False):
        from auto_patch.config import APRON_MAX_GRADE
        return float(APRON_MAX_GRADE)
    # TAXIWAY-EDGE ADOPTION (USER RULING 2026-07-07): a service-road
    # portion inside/alongside a taxiway follows the taxiway cap (1.5 %,
    # letter-aware via the adjacent taxiway's code letter).
    if getattr(s, "adopts_taxi_grade", False):
        from auto_patch.config import taxi_grade_cap_for_letter
        return float(taxi_grade_cap_for_letter(
            getattr(s, "adopted_taxi_letter", None)))
    letter = taxi_shape_code_letter(layout, s)
    if letter is not None:
        return float(taxi_grade_cap_for_letter(letter))
    return _role_grade(s.role)


def _open_ring(coords) -> list[tuple[float, float]]:
    if coords and coords[0] == coords[-1]:
        return list(coords[:-1])
    return list(coords)




# Relief iteration budget — umbrella ceiling for the within-bands convergence.






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
        ls = entry.line if hasattr(entry, "line") else (entry[0] if isinstance(entry, (tuple, list)) else entry)
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






def _apron_back_band_nodes(layout, bucket_to_idx):
    """Back-band = the BUILDING-FACING apron (user 2026-06-14, Phase B): every
    apron vertex that is CLOSER to a building frontage than to a taxi corridor.
    docs/apron_terminal_attraction_plan.md.

    This covers ALL apron around a building — the frontage, the gaps BETWEEN
    buildings, AND the hill-cut side (the convex-hull-pair definition missed the
    hill side, which is where HECA terminal9's residual sat).  The taxi-facing
    apron (closer to a corridor) stays out of the band and keeps its 1 % tie to
    the taxiway, so "the majority of the apron stays at 1 %".  Back-band edges
    grade to ``APRON_BACK_EDGE_GRADE`` (4 %) and the back band is governed by
    the wider runway-route band (see the enforce), so it can RISE to meet the
    flat terminals.

    Returns a set of global node indices (empty when the gate is off / no
    buildings)."""
    if not APRON_BACK_EDGE_RAMPS or TAXI_SLACK_TERMINALS:
        # TAXI-SLACK supersedes back-edge ramps: there is NO relaxed back band
        # (the apron never grades at 4%).  Every apron vert is plane-attracted
        # to the FLEXED corridor plane and capped at the 1.5% law / 1% pref —
        # the corridors took the steepness, so the apron stays in grade.
        return set()
    cano = layout.canonical_points

    def _gidx(x, y):
        return bucket_to_idx.get(cano.get_or_add(float(x), float(y)))

    bld_polys = [s.polygon for s in layout.shapes
                 if s.role == ROLE_BUILDING and s.polygon is not None
                 and not s.polygon.is_empty]
    apron_xy: dict = {}
    for s in layout.shapes:
        if s.role != ROLE_APRON or s.polygon is None or s.polygon.is_empty:
            continue
        for x, y in s.polygon.exterior.coords:
            i = _gidx(x, y)
            if i is not None:
                apron_xy[i] = (float(x), float(y))
    if not bld_polys or not apron_xy:
        return set()
    try:
        from shapely.geometry import Point as _BPt
        from shapely.ops import unary_union as _bunion
    except Exception:                                  # pragma: no cover
        return set()
    bld_union = _bunion(bld_polys)
    segs = _corridor_segments(layout, include_roads=False)
    # Exact taxi-corridor distance up to a generous cell (beyond it the node is
    # far from every taxiway, so building-facing by default).
    far = 1000.0
    grid = _seg_grid(segs, far) if segs else None
    band: set = set()
    for i, (x, y) in apron_xy.items():
        d_bld = bld_union.distance(_BPt(x, y))
        d_taxi = (_corridor_point_distance(x, y, segs, grid, far)
                  if grid is not None else float("inf"))
        if d_bld < d_taxi:
            band.add(i)
    if _os.environ.get("O4_TERM_DEBUG") == "1":
        print(f"[backband] apron_idx={len(apron_xy)} "
              f"buildings={len(bld_polys)} building_facing_band={len(band)}")
    return band


# Taxi-rect vertices always seed the geodesic corridor field (the rect IS
# the corridor surface); their transverse offset to the axis is bounded by
# the half-width — beyond this something is wrong, don't seed.

# APRON_BACK_EDGE_RAMPS: max centroid separation for a building PAIR to define
# an inter-terminal corridor (the apron between them may grade at 4 %).  Bounds
# the pairwise convex hulls to genuinely-adjacent terminals.

# APRON_BACK_EDGE_RAMPS: two flat pads closer than this that can't both be flat
# at their own levels co-level DOWN to the lower neighbour (the larger yields)
# instead of both sloping — buildings sharing an apron frontage should sit at
# one level, the apron grading to meet the lowered end.

# TAXI_SLACK_TERMINALS: max min-vertex gap for two terminals to be candidates
# for one shared (co-levelled) cluster.  Within this reach they share apron
# frontage and CAN be considered for co-levelling, BUT they only actually merge
# when the apron between them cannot bridge their independent balanced levels at
# <= the apron grade (|ΔL| > apron_grade * gap).  A string of buildings spaced
# along a long corridor whose levels step gently (apron <= 1.5%) therefore stays
# INDEPENDENT and the apron slopes between them — only buildings whose level gap
# the apron can't span share one level.

# APRON_BACK_EDGE_RAMPS: max law-window INVERSION (metres) for which a terminal
# still flattens (at the inverted-window midpoint) instead of sloping — a mild
# serving-corridor conflict the user wants flat; larger conflicts slope.

# APRON_BACK_EDGE_RAMPS: max summed apron-excess (metres) a flatten may ADD and
# still be accepted — lets the apron grade to MEET a flat pad with marginal
# over-cap residual instead of reverting the pad to a slope; genuinely-
# infeasible pads (metres of free-apron excess) still exceed it and slope.

# Max move for the write-arbitration soft-terminus projection (the p10d
# J-tail lesson: an unbounded terminus move manufactures walls elsewhere).

# Max move for the terminal LEAF re-level toward the adjacent-apron median
# (bounded so one badly-pinned apron region cannot relocate a whole pad).










# Difference-constraint within-shape enforcement budget.  The Dijkstra bands
# (``_grade_bands``) do the heavy lifting directly (O(E·log V), ~6 ms — the
# shortest cap-path IS the fully-propagated hard-anchor constraint, no iteration);
# the band-clamp then applies it, and the residual soft↔soft projection converges
# to its floor in ~1–2 k sweeps (it plateaus — more does nothing).  Anything left
# at the plateau is structural / anchor-pinned and only an anchor flex can fix it.


























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


def _grade_graph_edges(s, coords, idx, ctx, ring_only=False):
    """Adapter: the single grade graph's per-edge ``(key, key, cap)`` for one
    apron/junction shape, converted to the solver's ``(i, j, cap*dist)`` edge
    contract.  Keys are node indices; a ring vertex with no index gets a unique
    sentinel key so it stays distinct and is filtered out of the result.

    ``ring_only`` (user 2026-07-05 flatness tier): ring-adjacent pairs only —
    the eager O(n) share of a flatness-certified shape; the full set is the
    shape's ``lazy_expand`` thunk (this same call without ``ring_only``)."""
    from auto_patch import grade_graph as GG
    keys = [i if i is not None else ("_n", p) for p, i in enumerate(idx)]
    gs = GG.GradeShape(role=s.role, ring=list(coords), keys=keys)
    sc = GG.shape_constraints_cached(id(s.polygon), gs, ctx,
                                     ring_only=ring_only)
    pos = {i: coords[p] for p, i in enumerate(idx) if i is not None}
    out = []
    for (a, b, cap) in sc.edges:
        pa, pb = pos.get(a), pos.get(b)
        if pa is None or pb is None:
            continue
        d = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
        # cap is a grade_law.Allowance → budget = cL·Δs∥ + cT·Δs⊥ (today Δs∥=d).
        out.append((a, b, cap.at(d, 0.0)))
    return out


# ── Flatness-certified lazy tier (user 2026-07-05) ───────────────────────────
# Grid pitch for the certificate's DEM sweep.  The production DEM is
# airport-smoothed BEFORE patch generation (Ortho4XP apt_smoothing_pix=8, a
# ~700 m blur; the standalone/test path replicates the exact same smoothing in
# elevation.py) — so a 25 m grid cannot straddle a terrain feature the blur has
# not already spread across many samples.  That smoothing premise is what makes
# SAMPLING a valid gradient bound here.
_FLAT_CERTIFICATE_GRID_M = 25.0
# Refuse to certify a shape whose bounding box would need more samples than
# this (certificate cost stays bounded; refusal just means eager generation).
_FLAT_CERTIFICATE_MAX_SAMPLES = 20000


def _certify_flat_shape(layout, shape, coords, dem, tile_lat, tile_lon,
                        rate_min):
    """Flatness CERTIFICATE for one soft shape (user 2026-07-05 flatness
    tier).  Returns the per-ring-vertex DEM seed values when the local DEM
    gradient is provably ≤ ``rate_min`` everywhere over the shape, else
    ``None`` (not certified → the caller generates the pair set eagerly).

    Soundness: every within-shape law budget for an apron/junction pair is at
    least ``APRON_MAX_GRADE · dist`` (aprons/frontage are the tightest 1 %
    class; ``grade_law.classify_pair`` only relaxes upward from there, and an
    anisotropic baked budget is ≥ the flat one).  ``rate_min`` is
    ``0.6 · APRON_MAX_GRADE`` — the 0.6 safety factor covers estimation slack,
    and the validator's ELEV_ROUNDING_NOISE_M (0.03 m) absorbs emit rounding
    on the sub-3 m chords this leaves near the cap.  So a DEM whose gradient
    is ≤ ``rate_min`` satisfies EVERY body pair at the seed, and the pairs
    need not exist until a node moves off that seed.

    Estimation = the shape's ring-adjacent vertex pairs (exact — these ARE law
    pairs) plus a ~25 m grid over the bounding box, both through the SAME
    sampler the node seeds use (``elevation._sample_dem`` with the layout
    anchor frame), so the returned seed values are bit-identical to what
    ``_seed_elevations`` / ``_sample_node_dem`` produce.  ANY sampling gap
    (off-tile, DEM error, oversized bbox) refuses the certificate — failing
    toward correctness, never toward a skipped constraint."""
    from auto_patch.elevation import _sample_dem

    ring_seed = []
    for (x, y) in coords:
        try:
            lat, lon = layout.m_to_ll(x, y)
            value = _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except _GEOM_EXC:
            return None
        if value is None or value != value:
            return None
        ring_seed.append(float(value))

    # Ring-adjacent vertex pairs — exact (these are the eager law pairs; the
    # certificate must be at least as strict as what it stands in for).
    count = len(coords)
    for p in range(count):
        q = (p + 1) % count
        (xa, ya), (xb, yb) = coords[p], coords[q]
        dist = math.hypot(xa - xb, ya - yb)
        if dist < 1e-6:
            continue
        if abs(ring_seed[p] - ring_seed[q]) > rate_min * dist:
            return None

    # Minimum BODY-pair distance (non-adjacent vertices), grid-bucketed
    # O(n): it sizes the slack-aware movement tolerance — a certified
    # pair at ≤ rate_min·d keeps 0.4·rate_full·d of slack, so both
    # endpoints may drift (tolerance) each before any body pair can
    # reach its budget (user 2026-07-05 tuning: the 1e-6 tolerance made
    # every certified shape expand on the first mm of apron smoothing).
    minimum_body_distance = float("inf")
    bucket_cell = 4.0
    vertex_buckets: dict = {}
    for p, (x, y) in enumerate(coords):
        vertex_buckets.setdefault(
            (int(x // bucket_cell), int(y // bucket_cell)), []).append(p)
    for (cell_x, cell_y), members in vertex_buckets.items():
        neighbourhood = []
        for off_x in (-1, 0, 1):
            for off_y in (-1, 0, 1):
                neighbourhood.extend(vertex_buckets.get(
                    (cell_x + off_x, cell_y + off_y), ()))
        for p in members:
            xa, ya = coords[p]
            for q in neighbourhood:
                if q <= p:
                    continue
                gap = q - p
                if gap == 1 or gap == count - 1:
                    continue    # ring-adjacent pairs are eager, not body
                xb, yb = coords[q]
                dist = math.hypot(xa - xb, ya - yb)
                if dist < minimum_body_distance:
                    minimum_body_distance = dist
    if minimum_body_distance > 3.0 * bucket_cell:
        # No body pair inside the bucket horizon → every body pair is
        # at least the horizon apart; the horizon is a valid (smaller)
        # lower bound and keeps the tolerance computation sound.
        minimum_body_distance = 3.0 * bucket_cell

    # ~25 m grid over the shape's bounding box (axis-neighbour gradients).
    try:
        min_x, min_y, max_x, max_y = shape.polygon.bounds
    except _GEOM_EXC:
        return None
    width, height = max_x - min_x, max_y - min_y
    steps_x = max(1, int(math.ceil(width / _FLAT_CERTIFICATE_GRID_M)))
    steps_y = max(1, int(math.ceil(height / _FLAT_CERTIFICATE_GRID_M)))
    if (steps_x + 1) * (steps_y + 1) > _FLAT_CERTIFICATE_MAX_SAMPLES:
        return None
    spacing_x = width / steps_x
    spacing_y = height / steps_y
    grid = []
    for grid_row in range(steps_y + 1):
        row_values = []
        y = min_y + grid_row * spacing_y
        for grid_col in range(steps_x + 1):
            x = min_x + grid_col * spacing_x
            try:
                lat, lon = layout.m_to_ll(x, y)
                value = _sample_dem(dem, tile_lat, tile_lon, lat, lon)
            except _GEOM_EXC:
                return None
            if value is None or value != value:
                return None
            row_values.append(float(value))
        grid.append(row_values)
    for grid_row in range(steps_y + 1):
        for grid_col in range(steps_x + 1):
            here = grid[grid_row][grid_col]
            if grid_col < steps_x and spacing_x > 1e-6:
                if abs(grid[grid_row][grid_col + 1] - here) \
                        > rate_min * spacing_x:
                    return None
            if grid_row < steps_y and spacing_y > 1e-6:
                if abs(grid[grid_row + 1][grid_col] - here) \
                        > rate_min * spacing_y:
                    return None
    return ring_seed, minimum_body_distance


# ── Flat-airport fast path — Tier 1 certificate extensions (spec §3.2) ───────
# The per-airport certificate counter.  ``layout._flat_certificate_counts`` is a
# ``{class: {"certified", "expanded", "refused", "candidate"}}`` tally printed
# once per build (see ``_report_flat_certificate_counts``); every gate mutation
# runs through this helper so a mis-firing gate is visible in the console and in
# replays (spec §2.7 "no silent caps").
_FLAT_CERTIFICATE_CLASSES = ("rect", "apron", "junction", "seat")


def _flat_certificate_counter(layout):
    """Return the per-airport certificate tally on ``layout`` (created once)."""
    counts = getattr(layout, "_flat_certificate_counts", None)
    if counts is None:
        counts = {cls: {"certified": 0, "expanded": 0, "refused": 0,
                        "candidate": 0}
                  for cls in _FLAT_CERTIFICATE_CLASSES}
        try:
            layout._flat_certificate_counts = counts
        except (AttributeError, TypeError):               # pragma: no cover
            pass
    return counts


def _record_flat_certificate(layout, cls, outcome):
    """Increment the ``outcome`` (certified / expanded / refused / candidate)
    tally for shape ``cls``.  Silent no-op if the layout cannot carry state
    (never worth failing a build over a counter)."""
    try:
        _flat_certificate_counter(layout)[cls][outcome] += 1
    except (AttributeError, TypeError, KeyError):          # pragma: no cover
        pass


def _reset_flat_certificate_class(layout, cls):
    """Zero one class's tally before a fresh constraint build re-counts it —
    ``_build_shape_constraints`` runs several times per solve (construct,
    solve passes, projection), so the LAST pass's numbers are the ones the
    summary reports rather than an accumulation across passes."""
    counts = _flat_certificate_counter(layout)
    if cls in counts:
        expanded = counts[cls]["expanded"]     # expansions accrue during solve
        counts[cls] = {"certified": 0, "expanded": expanded,
                       "refused": 0, "candidate": 0}


def _report_flat_certificate_counts(layout, icao=""):
    """Print the one per-airport certificate summary line (spec §2 item 7).

    Reports certified / expanded / refused / candidate per shape class.
    ``certified`` and ``refused`` reflect the most recent constraint build +
    the building-seat pass; ``expanded`` is the running count of certificates
    the solve later had to void (incremented by the lazy-expand thunks), so a
    line printed before the solve shows 0 there — the tally on the layout keeps
    climbing and is readable post-solve for anyone inspecting a replay."""
    counts = getattr(layout, "_flat_certificate_counts", None)
    if not counts:
        return
    parts = []
    for cls in _FLAT_CERTIFICATE_CLASSES:
        c = counts.get(cls)
        if not c or not (c["certified"] or c["refused"] or c["candidate"]):
            continue
        parts.append(f"{cls} certified={c['certified']} "
                     f"expanded={c['expanded']} refused={c['refused']} "
                     f"of {c['candidate']} candidate(s)")
    if not parts:
        return
    prefix = f"  [flat-certificate] {icao}: " if icao else "  [flat-certificate] "
    print(prefix + "; ".join(parts))


def _certify_flat_rect(layout, shape, coords, cross_positions, axial_positions,
                       cap, dem, tile_lat, tile_lon, rate_factor):
    """Flatness CERTIFICATE for a clean 4-corner taxi rect (spec §3.2).

    A rect grades its two AXIAL (sloping) edges at ``cap · length`` and holds
    its two CROSS (flat-end) edges at cap≈0.  It certifies when the airport-
    smoothed DEM under it is provably flat enough that BOTH families are
    already satisfied at the per-vertex DEM seed:

      * every AXIAL edge's DEM relief is ≤ ``rate_factor · cap · length`` — the
        same slack-aware bound the apron/junction tier uses, and
      * every CROSS edge's DEM relief is ≤ ``RECT_CROSS_FLATNESS_TOLERANCE_M``
        (the flat-cross tolerance plus the smoothing reserve — a real
        cross-fall refuses), and
      * a ~25 m DEM grid over the bounding box has no direction steeper than
        ``rate_factor · cap`` (catches a mid-rect bump both corners miss — the
        SAME grid discipline and refusal-on-any-gap rule as
        ``_certify_flat_shape``).

    Returns ``(ring_seed_by_vertex, minimum_axial_length)`` — the per-corner
    DEM seed values (bit-identical to ``_seed_elevations``) plus the shortest
    axial edge (sizes the movement tolerance) — or ``None`` (refuse → the rect
    keeps its eager axial edges).  ``cross_positions`` / ``axial_positions``
    are ``(corner_index_a, corner_index_b)`` / ``(a, b, length)`` tuples into
    ``coords``; the caller has already labelled them by ``source_axis``
    projection.  ANY sampling gap refuses — fail toward correctness."""
    from auto_patch.elevation import _sample_dem

    ring_seed = []
    for (x, y) in coords:
        try:
            lat, lon = layout.m_to_ll(x, y)
            value = _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except _GEOM_EXC:
            return None
        if value is None or value != value:
            return None
        ring_seed.append(float(value))

    # CROSS (flat-end) edges: DEM relief within the flat-cross reserve.
    for (a, b) in cross_positions:
        if abs(ring_seed[a] - ring_seed[b]) > RECT_CROSS_FLATNESS_TOLERANCE_M:
            return None

    # AXIAL (sloping) edges: DEM relief within rate_factor of the axial budget.
    axial_rate = rate_factor * cap
    minimum_axial_length = float("inf")
    for (a, b, length) in axial_positions:
        if length < 1e-6:
            continue
        if abs(ring_seed[a] - ring_seed[b]) > axial_rate * length:
            return None
        if length < minimum_axial_length:
            minimum_axial_length = length
    if minimum_axial_length == float("inf"):
        return None                    # no measurable axial span → refuse

    # ~25 m DEM grid over the bounding box, every axis-neighbour gradient
    # within the axial rate (mirrors _certify_flat_shape; refuse on any gap).
    try:
        min_x, min_y, max_x, max_y = shape.polygon.bounds
    except _GEOM_EXC:
        return None
    width, height = max_x - min_x, max_y - min_y
    steps_x = max(1, int(math.ceil(width / _FLAT_CERTIFICATE_GRID_M)))
    steps_y = max(1, int(math.ceil(height / _FLAT_CERTIFICATE_GRID_M)))
    if (steps_x + 1) * (steps_y + 1) > _FLAT_CERTIFICATE_MAX_SAMPLES:
        return None
    spacing_x = width / steps_x
    spacing_y = height / steps_y
    grid = []
    for grid_row in range(steps_y + 1):
        row_values = []
        y = min_y + grid_row * spacing_y
        for grid_col in range(steps_x + 1):
            x = min_x + grid_col * spacing_x
            try:
                lat, lon = layout.m_to_ll(x, y)
                value = _sample_dem(dem, tile_lat, tile_lon, lat, lon)
            except _GEOM_EXC:
                return None
            if value is None or value != value:
                return None
            row_values.append(float(value))
        grid.append(row_values)
    for grid_row in range(steps_y + 1):
        for grid_col in range(steps_x + 1):
            here = grid[grid_row][grid_col]
            if grid_col < steps_x and spacing_x > 1e-6:
                if abs(grid[grid_row][grid_col + 1] - here) \
                        > axial_rate * spacing_x:
                    return None
            if grid_row < steps_y and spacing_y > 1e-6:
                if abs(grid[grid_row + 1][grid_col] - here) \
                        > axial_rate * spacing_y:
                    return None
    return ring_seed, minimum_axial_length


def _build_shape_constraints(layout, bucket_to_idx, ctx=None, dem=None,
                             tile_lat=0, tile_lon=0, hard_nodes=None,
                             defer_shape_ids=None):
    """Per-shape grade constraints for the directional relief: one entry per
    soft pavement shape with ``{nodes, edges, flat}`` — its node indices, its
    OWN internal grade edges ``(i, j, cap_m)``, and whether it must stay flat
    (terminal).  Rects use flat-cross (cap≈0) + axial edges; aprons use the
    in-pavement VISIBILITY graph (geodesic, see ``_visible_grade_edges``);
    junction/seam-rect use all-pair; terminal is flat.  Runway/seam are HARD,
    not included.

    ``ctx``: optionally a prebuilt ``grade_graph.build_context`` — pass the
    SAME one ``build_unified_graph`` will use so the per-shape law memo
    (``grade_graph.shape_constraints_cached``) computes each shape once.

    FLATNESS-CERTIFIED LAZY TIER (user 2026-07-05, gate ``O4_FLAT_SHAPE_LAZY``
    default on; needs ``dem``/``tile_lat``/``tile_lon`` + ``hard_nodes`` from
    the caller): an apron/junction shape whose local DEM gradient is provably
    below ``0.6 · APRON_MAX_GRADE`` (see ``_certify_flat_shape``) gets only
    its O(n) ring-adjacent pairs eagerly; its entry additionally carries
      * ``lazy_expand`` — thunk returning the FULL ``(i, j, budget)`` list
        (the exact eager generation, memoised via
        ``grade_graph.shape_constraints_cached``),
      * ``lazy_nodes`` / ``lazy_seed`` — the node indices and their DEM seed
        values at certificate time,
      * ``lazy_certified`` — permanent marker (hit-rate reporting).
    ``one_solve.feasibility_project`` expands the entry the moment any of its
    nodes moves off the seed; until then the certificate proves every body
    pair satisfied.  Shapes touching a ``hard_nodes`` member (runway / seam /
    join — they sit at profile values, not the DEM seed) are never certified.

    SCOPED FINAL PROJECTION (user 2026-07-05, ``O4_SCOPED_FINAL_PROJECTION``):
    ``defer_shape_ids`` — apron/junction shapes (by ``id(s)``) the caller has
    PROVED unchanged since the solve's writeback (ring geometry identical +
    every node value identical + no law-context input touching them changed —
    see ``route_profile.solve._scoped_projection_defer_ids``).  Their full
    pair set was already enforced by the solve on identical rings/values, so
    the entry is a pure lazy stub: NO eager edges, ``lazy_expand`` = the same
    full generation, movement tolerance 0 (any node the projection moves
    voids the proof and expands the entry through the standard lazy
    machinery).  The caller MUST fill ``lazy_seed`` with its own seed values
    before projecting (left ``None`` here so an unfilled stub fails loudly).
    Terminals, rects and service junctions keep their eager branches (they are
    flat / cheap / all-pair-small; the O(n²) cost lives in apron/junction).
    NOTE: ``one_solve._build_adjacency`` consequently sees only the ring edges
    of a certified shape for its neighbour-cap slabs — acceptable: that is a
    heuristic bound and every node is re-projected against the full law
    afterwards (the projection expands on first movement)."""
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
    # APRON BACK-EDGE RAMPS (user 2026-06-13): the back strip of an apron
    # (building frontage + gaps between buildings) may grade at the steeper
    # APRON_BACK_EDGE_GRADE so pads stay flat (docs/apron_back_edge_ramps.md).
    # Computed once and stashed for the enforce's coupled touches (band slack,
    # corridor-plane attractor skip, pairwise pad cap).  Empty set / no-op
    # when the gate is off → byte-identical.
    back_band = _apron_back_band_nodes(layout, bucket_to_idx)
    layout._apron_back_band = back_band
    # Single grade graph (docs/single_grade_graph.md): build the apron/junction
    # within-shape constraints from the ONE shared generator the validator also
    # uses.  Built once per solve; gate OFF → legacy _visible_grade_edges branch.
    from auto_patch import grade_graph as _GG
    _gg_ctx = ctx if ctx is not None else _GG.build_context(layout, bucket_to_idx)
    back_scale = (APRON_BACK_EDGE_GRADE / APRON_MAX_GRADE
                  if APRON_MAX_GRADE > 0 else 1.0)
    # FLATNESS-CERTIFIED LAZY TIER (user 2026-07-05): active only when the
    # caller supplies the DEM (certificate source) and the hard node set (a
    # shape touching a runway/seam/join member is never certified — those
    # nodes sit at profile values, not the DEM seed).  ``O4_FLAT_SHAPE_LAZY=0``
    # → exact old behaviour (everything generated eagerly).
    flat_lazy_enabled = (
        _os.environ.get("O4_FLAT_SHAPE_LAZY", "1") == "1"
        and dem is not None and hard_nodes is not None)
    # Certificate rate is PER SHAPE (user 2026-07-05 tuning): aprons and any
    # shape hosting building-pad keys certify against the tightest 1 % class;
    # a JUNCTION with no pad keys can never earn a pair budget below the
    # 1.5 % taxi body cap (frontage clamp needs a pad key; one-seam pairs
    # take the body cap; road-carve/aniso only relax upward), so it
    # certifies at 0.6 · 1.5 % = 0.9 % — at KDFW the global 0.6 % threshold
    # sat below the field's local gradients and certified almost nothing.
    # 0.6 = safety factor; the validator's ELEV_ROUNDING_NOISE_M (0.03 m)
    # absorbs emit rounding near the cap (see _certify_flat_shape).  The 0.6 is
    # now the single-source ``FLATNESS_CERTIFICATE_RATE_FACTOR`` in config.py
    # (value unchanged) so rects, seats and this apron/junction path share ONE
    # number (spec §2.5).
    flat_safety_factor = FLATNESS_CERTIFICATE_RATE_FACTOR
    flat_certified_count = 0
    flat_candidate_count = 0
    # Per-airport certificate tally (spec §2 item 7): a fresh constraint build
    # re-counts each soft class, so zero the apron/junction/rect classes here
    # (expansions accrued during any prior solve pass are preserved) and let the
    # branches below record certified / refused / candidate.  ``seat`` is filled
    # by ``building_feasibility.building_feasible_levels``.
    if flat_lazy_enabled:
        for _cert_cls in ("rect", "apron", "junction"):
            _reset_flat_certificate_class(layout, _cert_cls)
    # Taxi-rect certificate coverage (Tier 1, spec §3.2): active under the same
    # DEM/hard-node preconditions as the apron/junction tier, gated by
    # ``O4_FLAT_CERTIFICATE_COVERAGE`` (config default ON; read at call time so
    # the A/B inertness harness / tests can toggle it in-process).  A certified
    # rect keeps its flat-cross edges (cross coupling stays byte-identical) and
    # defers its two AXIAL edges to a lazy thunk — the solve enforces them the
    # moment a corner drifts.
    rect_certificate_enabled = (
        flat_lazy_enabled and FLAT_CERTIFICATE_COVERAGE
        and _os.environ.get("O4_FLAT_CERTIFICATE_COVERAGE", "1") != "0")
    # Node indices on a clean sloping-rect PLANE (4-corner, altitude_high/low).
    # Used to grade a rect end-cap as a PLANAR EXTENSION of its parent rect
    # (O4_CAP_PLANAR): the cap's inner edge sits on these nodes.
    # Default ON (user 2026-06-19, for in-sim test): grade rect end-caps as a
    # planar extension of their parent rect so the rect+cap tilt as one plane
    # and the cap-adjacent junctions co-solve flat (CYXY 4→0, SPJC 44→0; SPLP
    # neutral).  ⚠ HECA REGRESSES (741→883) — the terminal-canyon caps where
    # the rect can't tilt over-constrain; investigate.  O4_CAP_PLANAR=0 reverts.
    _cap_planar = _os.environ.get("O4_CAP_PLANAR", "1") == "1"
    rect_plane_idx: set = set()
    if _cap_planar:
        for s in layout.shapes:
            if (s.role in SLOPING_RECT_ROLES and s.node_altitudes is None
                    and s.polygon is not None and not s.polygon.is_empty):
                _c = _open_ring(list(s.polygon.exterior.coords))
                if len(_c) != 4:
                    continue
                for (x, y) in _c:
                    k = bucket_to_idx.get(
                        layout.canonical_points.get_or_add(float(x), float(y)))
                    if k is not None:
                        rect_plane_idx.add(k)
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
        cap = _shape_grade(layout, s)
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
        # APRON-FOLLOWS model: pads are TRANSPARENT — graded shapes at the
        # terminal cap through the visibility graph, exactly like an apron
        # (no cap-0 rigidity for ANY pad; flatness is imposed post-solve by
        # the INHERIT step from the settled median, see the enforce).
        if (s.role == ROLE_BUILDING and not TERMINAL_PADS_SLOPE
                and not TERMINAL_NATURAL_LEVELS):
            _sloped = getattr(layout, "_sloped_terminal_nodes", None)
            if not (_sloped and any(i in _sloped for i in nodes)):
                cap = 0.0
        flat = (cap <= 0.0)
        edges: list[tuple[int, int, float]] = []
        flat_pairs: list[tuple[int, int]] = []   # rect flat-end coupled pairs
        lazy_extras = None                       # flatness-certified lazy keys
        # A clean PLANAR rect (altitude_high/low) gets the flat-cross + axial
        # constraints.  A per-vertex ``node_altitudes`` piece is NOT planar —
        # e.g. the tile-cut seam WEDGE that follows the seam terrain's cross-
        # slope and blends back to the rect (user 2026-05-28) — so it must use
        # the all-pair rule, NOT a cap-0 flat end (which would force its two
        # seam-pinned corners equal and read as a spurious 3.3 m violation).
        is_rect = s.role in SLOPING_RECT_ROLES and len(coords) == 4 \
            and all(i is not None for i in idx) \
            and s.node_altitudes is None
        if (defer_shape_ids is not None and id(s) in defer_shape_ids
                and s.role in (ROLE_APRON, ROLE_JUNCTION)):
            # SCOPED FINAL PROJECTION deferral (see docstring): proven-
            # unchanged shape → lazy stub with only its O(n) RING-ADJACENT
            # pairs eager (same shape as the certificate tier: the ring pairs
            # keep the reach envelope's paths and the worklist's edge order
            # near the changed/unchanged interfaces close to the full
            # rebuild's).  The thunk is the exact eager generation (memoised
            # via grade_graph.shape_constraints_cached on the shared ctx), so
            # an expansion enforces the identical pair set.
            edges.extend(_grade_graph_edges(s, coords, idx, _gg_ctx,
                                            ring_only=True))
            deferred_node_indices = []
            seen_deferred_nodes = set()
            for node_index in idx:
                if node_index is None or node_index in seen_deferred_nodes:
                    continue
                seen_deferred_nodes.add(node_index)
                deferred_node_indices.append(node_index)
            lazy_extras = {
                "lazy_expand": (lambda _shape=s, _coords=coords, _idx=idx,
                                _law_ctx=_gg_ctx:
                                _grade_graph_edges(_shape, _coords, _idx,
                                                   _law_ctx)),
                "lazy_nodes": deferred_node_indices,
                "lazy_seed": None,       # caller fills from ITS seed values
                "lazy_move_tolerance": 0.0,
                "lazy_scoped": True,
            }
        elif flat:
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
            axial_edge_list: list[tuple[int, int, float]] = []
            axial_positions: list[tuple[int, int, float]] = []
            cross_positions: list[tuple[int, int]] = []
            rect_edges_wellformed = True
            for n, (_par, a, b, el) in enumerate(scored):
                if idx[a] is None or idx[b] is None or idx[a] == idx[b]:
                    rect_edges_wellformed = False
                    continue
                if n < 2:                # the two most-perpendicular = flat ends
                    edges.append((idx[a], idx[b], 0.0))
                    flat_pairs.append((idx[a], idx[b]))
                    cross_positions.append((a, b))
                else:                    # the two most-parallel = sloping edges
                    axial_edge_list.append((idx[a], idx[b], cap * el))
                    axial_positions.append((a, b, el))
            # FLAT-RECT CERTIFICATE (Tier 1, spec §3.2): when the airport-
            # smoothed DEM under the rect is provably flat, DEFER its two axial
            # (sloping) edges — proven satisfied at the DEM seed — to a lazy
            # thunk; the flat-cross edges above stay eager so the cross coupling
            # is byte-identical.  ``feasibility_project`` re-enforces the axial
            # edges the moment a corner drifts off its seed (expand → the exact
            # eager edge set), so a mis-certified rect fails toward correctness.
            rect_is_candidate = (
                rect_certificate_enabled and rect_edges_wellformed
                and len(axial_positions) == 2 and len(cross_positions) == 2
                and not any(i in hard_nodes for i in nodes))
            rect_certificate = None
            if rect_is_candidate:
                _record_flat_certificate(layout, "rect", "candidate")
                try:
                    rect_certificate = _certify_flat_rect(
                        layout, s, coords, cross_positions, axial_positions,
                        cap, dem, tile_lat, tile_lon, flat_safety_factor)
                except _GEOM_EXC:
                    rect_certificate = None
            if rect_certificate is not None:
                rect_seed_by_vertex, minimum_axial_length = rect_certificate
                # Slack-aware movement tolerance: axial edges sit at
                # ≤ rate_factor·cap·len, leaving (1−rate_factor)·cap·len of
                # slack; two endpoints drifting ``tolerance`` each consume
                # 2·tolerance, so tolerance = 0.2·cap·len_min keeps every axial
                # edge inside its budget without expansion (the apron tier's
                # rule, applied to the rect's own axial span).
                rect_move_tolerance = min(
                    0.02, max(1e-6, 0.2 * cap * minimum_axial_length))
                _record_flat_certificate(layout, "rect", "certified")
                rect_lazy_nodes: list[int] = []
                rect_lazy_seeds: list[float] = []
                seen_rect_nodes: set = set()
                for vertex_position, node_index in enumerate(idx):
                    if node_index is None or node_index in seen_rect_nodes:
                        continue
                    seen_rect_nodes.add(node_index)
                    rect_lazy_nodes.append(node_index)
                    rect_lazy_seeds.append(rect_seed_by_vertex[vertex_position])

                def _expand_rect(_layout=layout,
                                 _edges=list(axial_edge_list)):
                    _record_flat_certificate(_layout, "rect", "expanded")
                    return list(_edges)

                lazy_extras = {
                    "lazy_expand": _expand_rect,
                    "lazy_nodes": rect_lazy_nodes,
                    "lazy_seed": rect_lazy_seeds,
                    "lazy_move_tolerance": rect_move_tolerance,
                    "lazy_certified": True,
                    "lazy_rect": True,
                }
            else:
                if rect_is_candidate:
                    _record_flat_certificate(layout, "rect", "refused")
                edges.extend(axial_edge_list)
        elif (_cap_planar and getattr(s, "is_rect_cap", False)
              and len([i for i in idx if i in rect_plane_idx]) >= 2
              and len([i for i in idx
                       if i is not None and i not in rect_plane_idx]) >= 2):
            # Rect end-cap → PLANAR EXTENSION of its parent rect (user
            # 2026-06-19).  INNER edge = the 2 nodes welded to the rect's flat
            # end (already the rect's coupled pair, so _build_level_coupling
            # unions them); OUTER edge = the 2 corners + the centreline node M.
            # Couple inner flat and outer flat, join them with axial edges at
            # the taxi cap → the rect+cap form one plane that TILTS as a unit so
            # the cap-adjacent junction co-solves flat (a free-junction cap
            # stays a rigid flat buffer that blocks the rect from tilting to the
            # network → the 0.1-0.4 m cap-adjacent grade violations).
            inner = [i for i in range(len(idx))
                     if idx[i] is not None and idx[i] in rect_plane_idx]
            outer = [i for i in range(len(idx))
                     if idx[i] is not None and idx[i] not in rect_plane_idx]
            edges.append((idx[inner[0]], idx[inner[1]], 0.0))
            flat_pairs.append((idx[inner[0]], idx[inner[1]]))
            for k in range(1, len(outer)):
                edges.append((idx[outer[0]], idx[outer[k]], 0.0))
                flat_pairs.append((idx[outer[0]], idx[outer[k]]))
            for ii in inner:
                jj = min(outer, key=lambda o: (coords[ii][0] - coords[o][0]) ** 2
                         + (coords[ii][1] - coords[o][1]) ** 2)
                d = math.hypot(coords[ii][0] - coords[jj][0],
                               coords[ii][1] - coords[jj][1])
                edges.append((idx[ii], idx[jj], cap * d))
        elif s.role in (ROLE_APRON, ROLE_JUNCTION):
            # SINGLE GRADE GRAPH: apron/junction within-shape edges from the ONE
            # shared generator (auto_patch.grade_graph) — junction = apron with a
            # spine+body model at the taxiway per-letter cap (no legacy per-axis
            # diagonal-skip).  GRADED terminals (ROLE_BUILDING) + service_junction
            # stay on the legacy branches below for now.
            #
            # FLATNESS-CERTIFIED LAZY TIER (user 2026-07-05): if the DEM is
            # provably flat under this shape, generate only the O(n)
            # ring-adjacent pairs now and defer the O(n²) body pairs to the
            # ``lazy_expand`` thunk (soundness invariant: the body pairs are
            # satisfied AT the DEM seed; ``feasibility_project`` generates
            # them the moment any node moves off it).  Certificate failure of
            # ANY kind — including an exception — falls back to eager
            # generation: a lazy bookkeeping error must never silently drop
            # law coverage.
            lazy_seed_by_vertex = None
            lazy_certificate = None
            shape_rate_full = APRON_MAX_GRADE
            _cert_class = "apron" if s.role == ROLE_APRON else "junction"
            if flat_lazy_enabled:
                flat_candidate_count += 1
                _record_flat_certificate(layout, _cert_class, "candidate")
                if not any(i in hard_nodes for i in nodes):
                    if (s.role != ROLE_APRON
                            and not any(i in _gg_ctx.building_keys
                                        for i in nodes)):
                        shape_rate_full = TAXI_MAX_GRADE
                    try:
                        lazy_certificate = _certify_flat_shape(
                            layout, s, coords, dem, tile_lat, tile_lon,
                            flat_safety_factor * shape_rate_full)
                    except _GEOM_EXC:
                        lazy_certificate = None
                if lazy_certificate is None:
                    _record_flat_certificate(layout, _cert_class, "refused")
            if lazy_certificate is not None:
                lazy_seed_by_vertex, minimum_body_distance = lazy_certificate
                # Slack-aware movement tolerance: certified pairs sit at
                # ≤ 0.6·rate·d, leaving 0.4·rate·d of slack; two endpoints
                # drifting ``tolerance`` each consume at most 2·tolerance,
                # so tolerance = 0.2·rate·d_min keeps EVERY body pair
                # inside its budget without expansion.  The 1e-6 tolerance
                # made apron-smoothing's mm nudges expand every certificate.
                lazy_move_tolerance = min(
                    0.02, max(1e-6, 0.2 * shape_rate_full
                              * minimum_body_distance))
                flat_certified_count += 1
                _record_flat_certificate(layout, _cert_class, "certified")
                edges.extend(_grade_graph_edges(s, coords, idx, _gg_ctx,
                                                ring_only=True))
                lazy_node_indices = []
                lazy_node_seeds = []
                seen_node_indices = set()
                for vertex_position, node_index in enumerate(idx):
                    if node_index is None or node_index in seen_node_indices:
                        continue
                    seen_node_indices.add(node_index)
                    lazy_node_indices.append(node_index)
                    lazy_node_seeds.append(lazy_seed_by_vertex[vertex_position])

                def _expand_apron_junction(
                        _shape=s, _coords=coords, _idx=idx, _law_ctx=_gg_ctx,
                        _layout=layout, _cls=_cert_class):
                    _record_flat_certificate(_layout, _cls, "expanded")
                    return _grade_graph_edges(_shape, _coords, _idx, _law_ctx)

                lazy_extras = {
                    "lazy_expand": _expand_apron_junction,
                    "lazy_nodes": lazy_node_indices,
                    "lazy_seed": lazy_node_seeds,
                    "lazy_move_tolerance": lazy_move_tolerance,
                    "lazy_certified": True,
                }
            else:
                edges.extend(_grade_graph_edges(s, coords, idx, _gg_ctx))
        elif s.role == ROLE_BUILDING:
            # In-pavement VISIBILITY graph for GRADED terminals (when
            # TERMINAL_MAX_GRADE > 0 — large near-flat pads, same as an apron).
            # The within-shape grade
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
            # W2_CLEAN_BANDS: NO distance window — enforce EVERY in-pavement
            # visible chord (matches the corrected validator).  A long apron
            # chord is the surface the aircraft sits on; enforcing it is also
            # what forces a high corridor to DESCEND so the apron can grade.
            vis_edges = _visible_grade_edges(
                coords, idx, cap, s.polygon,
                container=None,
                max_len=(None if W2_CLEAN_BANDS
                         else (ROUTE_FIELD_LOCAL_WINDOW_M if ROUTE_FIELD_MODEL
                               else None)))
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
        entry = {"nodes": nodes, "edges": edges, "flat": flat,
                 "flat_pairs": flat_pairs,
                 "area": float(s.polygon.area),
                 "role": s.role,
                 "ref": s.ref or ""}
        if lazy_extras is not None:
            entry.update(lazy_extras)
        out.append(entry)
    if flat_lazy_enabled and _os.environ.get("O4_STEP_DEBUG") == "1":
        print(f"  [flat-lazy] certified {flat_certified_count} of "
              f"{flat_candidate_count} apron/junction shape(s) "
              f"(safety factor {flat_safety_factor:.2f}, per-role rates)")
    return out






















# A junction vertex within this distance of a sloping rect's / runway's
# long edge is treated as HUGGING/SHADOWING it (the vertex-push pass keeps
# a designed 1.0 m standoff; accumulated drift puts shadow-edge endpoints
# at up to ~1.8 m — SPJC's stepping endpoint sat 1.74 m off) and takes the
# edge-plane altitude, so a straight shadowing edge lerps along the plane.




# Route-distance measurement uncertainty, as a fraction of the route length.
# The taxi-route graph under-counts real taxi paths: endpoint stubs are
# straight chords (the centerline rows stop short of runway edges) and row
# joins are uncurved corners (no fillets).  Measured at HECA's A4↔T4 corridor
# (s73): graph 3,217 m vs the ≥3,353 m reality requires — ~4 % short.  A route
# demand below ``frac · cap · route_d`` is within measurement noise of a
# feasible corridor; the demand synthesis drops it instead of flexing a runway.
# Value lives in config (single source of truth — the route-field bands and
# the validator use the SAME margin); re-exported under the historical name.






# Continuation gate: the exit direction, the across-junction gap vector and
# the next rect's entry direction must all agree within 45°.




# Tile-cut setback (m): pavement is cut back this far from each integer tile
# line (tile_cut.cut_layout_at_tile_boundaries half_width_m).  The taxi-route
# field anchors the seam at the SETBACK (where the pavement actually ends),
# not at the integer boundary — same model as the runway setback pin (user
# 2026-06-20): every node at the setback sits at its own DEM, a threshold.












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
    # TERRAIN-ROLE ADMISSION (Slice B Stage B0, docs/slice_b_solver_absorption_
    # design.md): gate ON, the admitted terrain graph roles join the registry
    # and node list exactly the way the object-bridge plate roles do (they are
    # already in PAVEMENT_ROLES).  The admitted set is EMPTY by default (and
    # whenever the master gate is off), so ``node_roles is PAVEMENT_ROLES`` and
    # the iteration — and every node index it assigns — is byte-identical to
    # today.  (Until stages B1-B3 move construction pre-solve these shapes are
    # not even present in ``layout.shapes`` at solve time, so admitting the
    # roles is doubly inert; the hook is the structural seam those stages fill.)
    # Admission is (role, ref)-keyed (B3 order 1): a shape enters the node
    # list if it is a pavement role (always, as today) OR its (role, ref)
    # family is admitted this build.  With an empty admitted family set the
    # iteration — and every node index it assigns — is byte-identical to
    # today.  Terrain roles are disjoint from PAVEMENT_ROLES, so the two
    # clauses never overlap.
    _admitted_refs = admitted_terrain_refs()
    for s in layout.shapes:
        if s.role not in PAVEMENT_ROLES and (
                s.role, getattr(s, "ref", None)) not in _admitted_refs:
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
    # GAP-FILL SPINE ADMISSION (Slice B Stage B2, ratified mechanism
    # 2026-07-10; docs/slice_b_solver_absorption_design.md §B2): the
    # drainage-spine vertices are INTERIOR points of the gap faces —
    # they lie on no shape ring (the OPEN-WAY design floats them >= 2 m
    # off every boundary), so the ring iteration above can never admit
    # them.  When the gap sub-gate admits ROLE_GRADED_STRIP, the
    # pre-solve construction store (``layout.gap_fill_presolve``, built
    # by ``gap_fill.construct_gap_fill_presolve`` before the solve)
    # supplies them here as FREE solver variables.  They intern through
    # the same canonical registry (0.5 m) — spine stations sit
    # ``GAP_FILL_SPINE_STEP_M`` (15 m) apart and >= 2 m off every ring,
    # so no spine node can merge with another node's bucket.  Gate OFF
    # (or no store) this loop body never runs — byte-inert.
    if (ROLE_GRADED_STRIP, "gap_fill_spine") in _admitted_refs:
        for _gap_entry in (getattr(layout, "gap_fill_presolve", None)
                           or ()):
            for x, y in _gap_entry.get("spine", ()):
                k = layout.canonical_points.get_or_add(float(x), float(y))
                if k not in bucket_to_idx:
                    bucket_to_idx[k] = len(nodes)
                    nodes.append((float(x), float(y)))
    # ADJACENT-GROUND ZONE-ROW ADMISSION (Slice B stage B3 order 2):
    # the band zone-row vertices (every band row at lateral distance
    # > 0 from the pavement ring — the lip row, the graded-width row,
    # the daylight row) become FREE solver variables.  Their geometry
    # was marched pre-solve by ``adjacent_ground.construct_adjacent_
    # ground_presolve`` (the order-1 construct store, schema-split at
    # order 2 to carry the ``zone_nodes`` grid).  They intern through
    # the same canonical registry (0.5 m).  The FIRST zone index is
    # stashed on the layout so the constraint builder can classify a
    # zone node whose bucket was already claimed by a PAVEMENT or
    # gap-spine node (identity adoption — no band edge may constrain a
    # pavement variable; pavement value always wins as an identity).
    # Gate OFF (or no store): the loop body never runs — byte-inert.
    layout._adjacent_ground_first_zone_index = len(nodes)
    if (ROLE_GRADED_STRIP, "adjacent_ground") in _admitted_refs:
        for _band_entry in (getattr(layout, "adjacent_ground_presolve",
                                    None) or ()):
            for _zone_node in _band_entry.get("zone_nodes", ()):
                x, y = _zone_node["xy"]
                k = layout.canonical_points.get_or_add(float(x), float(y))
                if k not in bucket_to_idx:
                    bucket_to_idx[k] = len(nodes)
                    nodes.append((float(x), float(y)))
    return nodes, bucket_to_idx


def _build_gap_spine_constraints(layout, bucket_to_idx, seed_elev=None):
    """Stage B2 constraint entries for the gap-fill drainage spines
    (ratified mechanism 2026-07-10; the LAW trace: today's analytic
    spine values obey exactly two invariants — each station inside its
    per-parent ``adjacent_ground_envelope`` interval, and longitudinal
    smoothness — and this builder encodes the FIRST as solver interval
    edges; the second is the ``TAXIWAY_MAX_GRADE_CHANGE_PER_M``
    second-difference fairing pass in ``route_profile.solve``, the
    project's own spine-curvature law, because ``ROLE_GRADE_LIMITS``
    holds NO first-difference cap for ``graded_strip`` and a
    second-difference cap is not expressible as a pairwise slab).

    Per spine node, per frozen parent spec (``gap_fill._freeze_spine_
    parent_specs``): ONE interval 4-tuple ``(spine_index,
    station_index, floor_offset, ceiling_offset)`` — the B0 signed slab
    ``floor_offset <= z_spine − z_station <= ceiling_offset`` with
    ``None`` sides preserved (the law's own open-side semantics).  The
    station index is the FROZEN-NEAREST pavement chain station mapped
    through the canonical registry.

    EMPTY-INTERSECTION RESOLUTION (measured 2026-07-10, first gate-ON
    CYXY build): two parents whose envelopes cannot be jointly
    satisfied give the POCS sweep an empty intersection — the two
    projections ping-pong the spine node (and, through the shared
    corrections, the stations' whole neighbourhoods) until the sweep's
    visit budget caps out (27.9 M worklist visits vs 30 k gate-OFF;
    Solving phase +23 s).  The analytic valuation had this exact case
    and RESOLVED it — ``gap_fill._spine_interval``: on an empty
    combined interval, the NEARER parent's interval alone applies.
    ``seed_elev`` (the ``_seed_elevations`` output) encodes that same
    law rule at build time: when a node's two parent intervals are
    already disjoint at the SEED station elevations (stations are
    pavement nodes that move little from seed), only the nearer
    (first — specs are ordered nearest-first) parent's edge is kept.
    ``seed_elev=None`` keeps every edge (unit tests).

    Returns ``(sc_entries, spine_index_set, chains)``; ``chains`` feeds
    the fairing pass (per chain: node indices, coordinates, and the
    resolved per-node interval specs for the envelope clamp — the
    fairing keeps its own LIVE nearer-parent fallback for conflicts
    that only appear as stations move)."""
    entries = getattr(layout, "gap_fill_presolve", None) or []
    cps = layout.canonical_points
    sc_out: list[dict] = []
    spine_idx: set[int] = set()
    chains: list[dict] = []
    n_pruned = 0
    for entry in entries:
        idx = [bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
               for (x, y) in entry["spine"]]
        edges: list[tuple] = []
        node_specs: list[list[tuple]] = []
        for i, specs in zip(idx, entry["specs"]):
            resolved: list[tuple] = []
            if i is not None:
                spine_idx.add(i)
                for (sx, sy), floor_off, ceil_off in specs:
                    j = bucket_to_idx.get(
                        cps.get_or_add(float(sx), float(sy)))
                    if j is None or j == i:
                        continue
                    resolved.append((j, floor_off, ceil_off))
                if (seed_elev is not None and len(resolved) == 2):
                    # Seed-time joint feasibility of the two parent
                    # slabs: floor = max over parents of (z_station +
                    # floor_offset), ceiling = min of (z_station +
                    # ceiling_offset); disjoint -> nearer parent wins
                    # (the analytic law's own empty-intersection rule).
                    lo_bound = None
                    hi_bound = None
                    for j, f_off, c_off in resolved:
                        if j >= len(seed_elev):
                            continue
                        zj = seed_elev[j]
                        if f_off is not None:
                            b = zj + f_off
                            lo_bound = b if lo_bound is None \
                                else max(lo_bound, b)
                        if c_off is not None:
                            b = zj + c_off
                            hi_bound = b if hi_bound is None \
                                else min(hi_bound, b)
                    if (lo_bound is not None and hi_bound is not None
                            and lo_bound > hi_bound):
                        resolved = resolved[:1]
                        n_pruned += 1
                edges.extend((i, j, f_off, c_off)
                             for j, f_off, c_off in resolved)
            node_specs.append(resolved)
        node_list = [i for i in idx if i is not None]
        if not node_list:
            continue
        sc_out.append({"nodes": node_list, "edges": edges, "flat": False,
                       "flat_pairs": (), "area": 0.0,
                       "role": ROLE_GRADED_STRIP,
                       "ref": "gap_fill_spine"})
        chains.append({"idx": idx, "xy": list(entry["spine"]),
                       "specs": node_specs})
    if n_pruned and _os.environ.get("O4_STEP_DEBUG") == "1":
        print(f"    [gap-spine] empty-intersection resolution: "
              f"{n_pruned} node(s) kept the nearer parent's interval "
              f"only (the analytic law's own fallback rule)")
    return sc_out, spine_idx, chains


def _build_adjacent_ground_zone_constraints(layout, bucket_to_idx):
    """Stage B3 order 2 constraint entries for the adjacent-ground band
    zone rows (ratified mechanism 2026-07-11; the LAW trace — the order-2
    scout refutation, recorded in the corrected design doc: the analytic
    band valuation is a PER-VERTEX two-sided envelope clamp of the DEM
    against the host-edge-referenced corridor,

        value = clamp(dem, edge + floor_offset(d), edge + ceiling_offset(d)),

    with NO neighbour coupling of any kind — ``config.ROLE_GRADE_LIMITS
    ['graded_strip'] is None``, and the only frontage coupling in the
    band machinery is the daylight benching of FOOTPRINT DEPTHS, which
    stays construction-side.  The encoding is therefore exactly ONE
    two-sided envelope interval edge per zone node to its frozen-nearest
    host pavement ring vertex (the B2 frozen-nearest pattern) plus the
    DEM seed ``_seed_elevations`` already provides: no transverse cross
    edges, no longitudinal edges, no fairing.  Projection of the DEM
    seed onto the signed slab IS the analytic clamp).

    IDENTITY-COLLISION RULE: a zone node whose canonical bucket resolves
    to a PRE-EXISTING solver node (a pavement ring vertex, a gap-spine
    node — index below ``layout._adjacent_ground_first_zone_index``)
    gets NO edge: the band ADOPTS that variable's value by identity
    (pavement value always wins at a pavement node — an identity, not
    an arbitration; a band law edge must never constrain a pavement
    variable).  A zone node whose bucket was already claimed by an
    EARLIER zone node (cross-row or cross-shape interning inside the
    0.5 m registry tolerance) also gets no second edge — the first
    claimant's corridor governs; attaching both could hand the POCS
    sweep two disjoint slabs on one variable (the measured B2
    empty-intersection ping-pong).  Both collision classes are counted
    and reported (the design doc's open-question-2 assertion).

    Returns ``(sc_entries, zone_idx_set, collision_counts)`` where
    ``collision_counts`` is ``(n_pavement_adopted, n_cross_claimed)``."""
    entries = getattr(layout, "adjacent_ground_presolve", None) or []
    first_zone = getattr(layout, "_adjacent_ground_first_zone_index", 0)
    cps = layout.canonical_points
    sc_out: list[dict] = []
    zone_idx: set[int] = set()
    claimed: set[int] = set()
    n_pavement_adopted = 0
    n_cross_claimed = 0
    for entry in entries:
        edges: list[tuple] = []
        node_list: list[int] = []
        for zone_node in entry.get("zone_nodes", ()):
            x, y = zone_node["xy"]
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if i is None:
                continue
            node_list.append(i)
            zone_idx.add(i)
            if i < first_zone:
                n_pavement_adopted += 1
                continue
            if i in claimed:
                n_cross_claimed += 1
                continue
            claimed.add(i)
            hx, hy = zone_node["host"]
            j = bucket_to_idx.get(cps.get_or_add(float(hx), float(hy)))
            if j is None or j == i:
                continue
            floor_off = zone_node["floor_off"]
            ceil_off = zone_node["ceil_off"]
            if floor_off is None and ceil_off is None:
                continue
            edges.append((i, j, floor_off, ceil_off))
        if not node_list:
            continue
        sc_out.append({"nodes": node_list, "edges": edges, "flat": False,
                       "flat_pairs": (), "area": 0.0,
                       "role": ROLE_GRADED_STRIP,
                       "ref": "adjacent_ground"})
    if _os.environ.get("O4_STEP_DEBUG") == "1" and (
            n_pavement_adopted or n_cross_claimed):
        print(f"    [adjacent-ground-zone] identity collisions: "
              f"{n_pavement_adopted} zone node(s) adopted a pre-existing "
              f"pavement/spine variable (no band edge), "
              f"{n_cross_claimed} interned with an earlier zone node "
              f"(first claimant's corridor governs)")
    return sc_out, zone_idx, (n_pavement_adopted, n_cross_claimed)




def _runway_edge_pts(layout, elev, bucket_to_idx, step_m=10.0):
    """``[(x, y, elev)]`` runway-EDGE anchor points for the shared
    route-feasibility band (``building_feasibility.reach_band_unified``) —
    DENSIFIED along the runway boundary (a point every ``step_m``, elevation
    interpolated along the edge).  A taxiway connects to a runway at any point on
    its EDGE — often MID-edge (the 02 threshold), far from a corner vertex — so
    anchoring only on the corners makes the band miss the real near connection
    and measure a long way round (the over-loose ceiling that let the 02→A2 spine
    rise too steep, field item 2/3).  Measure to the runway EDGE, not its
    corners."""
    cps = layout.canonical_points
    rwy_pts: list = []
    for s in layout.shapes:
        if (s.role != ROLE_RUNWAY or s.polygon is None
                or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        elevs = []
        for (x, y) in ring:
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            elevs.append(elev[i] if i is not None else None)
        m = len(ring)
        for a in range(m):
            b = (a + 1) % m
            (x0, y0), (x1, y1) = ring[a], ring[b]
            e0, e1 = elevs[a], elevs[b]
            if e0 is None:
                continue
            if e1 is None:
                rwy_pts.append((x0, y0, e0))
                continue
            seg = math.hypot(x1 - x0, y1 - y0)
            n_sub = max(1, int(seg // step_m))
            for t in range(n_sub):
                f = t / n_sub
                rwy_pts.append((x0 + f * (x1 - x0), y0 + f * (y1 - y0),
                                e0 + f * (e1 - e0)))
    # THRESHOLD MARKERS (user 2026-06-23): a runway whose END is absorbed into an
    # apron (CYXY 02) leaves the CIFP threshold ~200 m beyond the built pavement.
    # The runway is solved across its WHOLE profile, so extrapolate that profile
    # to the marker and anchor the route band THERE — otherwise a node 63 m from
    # the threshold measures the ~200 m route to the pavement and floats too high.
    rwy_pts.extend(_threshold_anchors(layout, elev, bucket_to_idx))
    return rwy_pts


def _threshold_anchors(layout, elev, bucket_to_idx):
    """``[(x, y, elev)]`` for each runway threshold MARKER, the runway profile
    extrapolated to the marker along the runway axis (linear least-squares fit of
    the built pavement's per-vertex elevations vs axis position).  For a runway
    whose end is built, this ≈ the pavement-end elevation (redundant, harmless);
    for an absorbed end it recovers the CIFP threshold elevation at the marker."""
    thr = getattr(layout, "runway_thresholds", None) or []
    cps = layout.canonical_points
    rwy_v: list = []
    for s in layout.shapes:
        if (s.role == ROLE_RUNWAY and s.polygon is not None
                and not s.polygon.is_empty):
            ring = _open_ring(list(s.polygon.exterior.coords))
            for (x, y) in ring:
                i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
                if i is not None:
                    rwy_v.append((x, y, elev[i]))
    out: list = []
    for k in range(0, len(thr) - 1, 2):
        ax0, ay0 = thr[k]
        bx0, by0 = thr[k + 1]
        dx, dy = bx0 - ax0, by0 - ay0
        L = math.hypot(dx, dy)
        if L < 1.0:
            continue
        ux, uy = dx / L, dy / L
        pts = []                         # (pos_along_axis, elev) on this runway
        for (x, y, e) in rwy_v:
            perp = abs((x - ax0) * uy - (y - ay0) * ux)
            pos = (x - ax0) * ux + (y - ay0) * uy
            if perp < 60.0 and -60.0 <= pos <= L + 60.0:
                pts.append((pos, e))
        if len(pts) < 2:
            continue
        nP = len(pts)
        sp = sum(p for p, _ in pts)
        se = sum(e for _, e in pts)
        spp = sum(p * p for p, _ in pts)
        spe = sum(p * e for p, e in pts)
        den = nP * spp - sp * sp
        if abs(den) < 1e-6:
            continue
        b = (nP * spe - sp * se) / den
        a = (se - b * sp) / nP
        # Only emit a marker for an ABSORBED end (built pavement does NOT reach the
        # CIFP threshold).  For a BUILT end the real runway edge vertices already
        # anchor the threshold at its true surface elevation, and the global
        # least-squares LINE extrapolates a runway with a flattened end BELOW that
        # surface (CYXY 14R: built end at 693.9, fit extrapolates 691.47) — a
        # spurious low anchor that becomes the nearest runway "contact" for a
        # taxiway joining there and drags its spine ~2.4 m under the runway (the
        # F/14R valley + steep join).  Skip a marker whose endpoint already has
        # built runway pavement within ``_BUILT_END_TOL_M``.
        _BUILT_END_TOL_M = 40.0
        if min((math.hypot(vx - ax0, vy - ay0) for (vx, vy, _e) in rwy_v),
               default=float("inf")) > _BUILT_END_TOL_M:
            out.append((ax0, ay0, a))              # marker A (absorbed end only)
        if min((math.hypot(vx - bx0, vy - by0) for (vx, vy, _e) in rwy_v),
               default=float("inf")) > _BUILT_END_TOL_M:
            out.append((bx0, by0, a + b * L))      # marker B (absorbed end only)
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
        from ..layout import SHARED_VERTEX_TOL_M
        from ..elevation import _sample_dem
        from ..seam_anchors import (SEAM_CLAMP_GRADE, SEAM_CLAMP_ROLES,
                                    runway_clamp_floor)
        bk_s = 1.0 / SHARED_VERTEX_TOL_M
        cps = layout.canonical_points
        # Gather every seam vertex FIRST (idx → position, stored-altitude
        # fallback, and which roles own it), THEN pin — the runway
        # skip/clamp below must not depend on which shape happens to
        # visit a shared bucket first.
        seam_pins: dict = {}     # idx -> [x, y, fallback_alt, airside, runway]
        for s in layout.shapes:
            if s.polygon is None or s.polygon.is_empty:
                continue
            coords = _open_ring(list(s.polygon.exterior.coords))
            if len(coords) < 3:
                continue
            # Per-vertex stored altitude (if any) is only a FALLBACK for when the
            # DEM is unavailable; do NOT gate on it.  A SOFT junction/apron
            # (node_altitudes unset pre-solve) still owns seam vertices that MUST
            # be hard-pinned — otherwise the body fill pulls the seam vertex up to
            # the route/network level (the SPLP tile-77 seam cliff: a junction
            # seam node seeded 72.2 SOFT → one_profile_solve raised it to 74.7,
            # while tile-78's apron at the same seam stayed at the DEM → 2.4 m
            # cross-tile step).
            alts = list(s.node_altitudes[:len(coords)]) if s.node_altitudes else None
            airside = s.role in SEAM_CLAMP_ROLES
            runway = s.role in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING)
            for vi, (x, y) in enumerate(coords):
                seam_bk = (int(round(x * bk_s)), int(round(y * bk_s)))
                if seam_bk not in seam_keys:
                    continue
                idx = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
                if idx is None:
                    continue
                fallback = None
                if alts is not None and vi < len(alts) \
                        and alts[vi] is not None:
                    fallback = float(alts[vi])
                rec = seam_pins.get(idx)
                if rec is None:
                    seam_pins[idx] = [x, y, fallback, airside, runway]
                else:
                    if rec[2] is None:
                        rec[2] = fallback
                    rec[3] = rec[3] or airside
                    rec[4] = rec[4] or runway
        # ── Phase 1: raw pin values ──────────────────────────────────
        # Seam vertex = the SMOOTHED DEM HARD anchor (user 2026-06-28,
        # never raw HGT): the surface meets terrain at the tile edge so
        # BOTH tiles pin the same seam point to the same value → cross-tile
        # continuity.  Re-sampled HERE (not trusted from node_altitudes) so
        # a seam vertex created by a LATE geometry pass is pinned too.
        # RUNWAY-owned buckets keep their hard-anchor values instead (the
        # redistributed FAA profile, already anchored to the seam DEM at
        # the centerline-boundary crossing) — re-sampling the DEM per
        # vertex carved the terrain into the runway at oblique seam
        # crossings (the SPLP 4.2 m V-notch; see
        # ``tile_cut._pin_runway_piece_to_profile``); they still act as
        # fixed SOURCES for the fairing envelope below.
        pin_vals: dict = {}      # idx -> value to write
        for idx, (x, y, fallback, airside, runway) in seam_pins.items():
            if runway and is_hard[idx]:
                pin_vals[idx] = float(elev[idx])
                continue
            v = None
            if dem is not None:
                try:
                    lat, lon = layout.m_to_ll(x, y)
                    sv = _sample_dem(dem, tile_lat, tile_lon, lat, lon)
                    if sv is not None and sv == sv:
                        v = float(sv)
                except Exception:                          # pragma: no cover
                    v = None
            if v is None:
                v = fallback             # DEM unavailable → stored fallback
            if v is None:
                continue
            # AIRSIDE pins take the runway clamp floor (user SPLP report
            # 2026-07-03): a raw-DEM pin below ``runway_e − cap·d`` makes
            # the pin↔runway chain infeasible and the final GS midpoints
            # the conflict into a V-notch.  Both tiles share the same
            # runways + profile → same floor → cross-tile continuity.
            if airside:
                try:
                    f = runway_clamp_floor(layout, x, y)
                except Exception:                          # pragma: no cover
                    f = None
                if f is not None and f > v:
                    v = f
            pin_vals[idx] = v

        # ── Phase 2: grade-project seam pins along the ring (pin↔pin law) ──
        # Raw per-pin DEM pins trace every terrain bump into the pavement
        # at the seam.  Pin↔pin pairs are exactly the class the
        # within-shape law exempts (both endpoints hard), so a local
        # terrain notch at the band edge emits as a pavement dip (SPLP:
        # mirrored 1.2 m junction dips at 2.9 % over 45 m) — and
        # cap-violating pins also make the interior solve infeasible
        # (each pin pulls its soft neighbours toward itself; the GS
        # midpoints the conflict).  Project the pin values onto the
        # pairwise polytope ``|v_i − v_j| ≤ cap·path`` over CONSECUTIVE
        # pins along each ring — soft intermediate vertices are skipped
        # but their path length counts: the law grades them onto the
        # pin-to-pin line, so the pins themselves must be mutually
        # cap-feasible over that path (adjacent-vertex-only pairs missed
        # pins separated by soft nodes — SPLP emitted a 2.5 % straight
        # line between two band-edge pins 45 m apart).  POCS, violation
        # split equally (the minimum-movement projection); runway pins
        # are IMMOVABLE profile authority, their neighbour takes the
        # whole move.  Ring paths keep the coupling inside one pavement
        # shape — no reach across grass gaps — and terrain adherence is
        # preserved wherever the DEM trace is already cap-legal (the
        # projection is identity there).  Deterministic: same rings,
        # same DEM, same sweep order on both tile builds.
        cap = SEAM_CLAMP_GRADE
        _PIN_PAIR_ROLES = SEAM_CLAMP_ROLES | {ROLE_RUNWAY,
                                              ROLE_RUNWAY_CROSSING}
        pin_edges: dict = {}     # (idx_lo, idx_hi) -> ring-path distance
        for s in layout.shapes:
            if s.role not in _PIN_PAIR_ROLES:
                continue
            if s.polygon is None or s.polygon.is_empty:
                continue
            coords = _open_ring(list(s.polygon.exterior.coords))
            n_ring = len(coords)
            if n_ring < 3:
                continue
            ring_idx = []
            for (x, y) in coords:
                idx = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
                ring_idx.append(idx if idx in pin_vals else None)
            pin_positions = [k for k in range(n_ring)
                             if ring_idx[k] is not None]
            if len(pin_positions) < 2:
                continue
            segment_lengths = [
                math.hypot(coords[(k + 1) % n_ring][0] - coords[k][0],
                           coords[(k + 1) % n_ring][1] - coords[k][1])
                for k in range(n_ring)]
            for pi in range(len(pin_positions)):
                k_a = pin_positions[pi]
                k_b = pin_positions[(pi + 1) % len(pin_positions)]
                idx_a = ring_idx[k_a]
                idx_b = ring_idx[k_b]
                if idx_a == idx_b:
                    continue
                path = 0.0
                k = k_a
                while k != k_b:
                    path += segment_lengths[k]
                    k = (k + 1) % n_ring
                if path < 0.5:
                    continue
                key = (idx_a, idx_b) if idx_a < idx_b else (idx_b, idx_a)
                prev = pin_edges.get(key)
                if prev is None or path < prev:
                    pin_edges[key] = path
        # ALSO couple CONSECUTIVE pins along each seam band edge across
        # shape boundaries: two pins a few metres apart owned by
        # DIFFERENT shapes get no ring edge, and the emitted surface
        # stepped 1.9 m between them (SPLP: 34 % across a junction↔apron
        # boundary on the band edge).  Pairwise POCS is inert at
        # distance (budget grows with separation), so grouping by
        # boundary line cannot re-create the one-sided-envelope hillside
        # lift.  Group key: boundary axis + integer line + side.
        pin_chains: dict = {}
        for idx, (x, y, fallback, airside, runway) in seam_pins.items():
            if idx not in pin_vals:
                continue
            try:
                lat, lon = layout.m_to_ll(x, y)
            except Exception:                              # pragma: no cover
                continue
            d_lat = lat - round(lat)
            d_lon = lon - round(lon)
            near_deg = 0.0003                # ~33 m: band edges sit ~5 m off
            if abs(d_lon) <= abs(d_lat) and abs(d_lon) <= near_deg:
                key = ('lon', int(round(lon)),
                       0 if d_lon == 0 else (1 if d_lon > 0 else -1))
                pin_chains.setdefault(key, []).append((y, idx))
            elif abs(d_lat) < abs(d_lon) and abs(d_lat) <= near_deg:
                key = ('lat', int(round(lat)),
                       0 if d_lat == 0 else (1 if d_lat > 0 else -1))
                pin_chains.setdefault(key, []).append((x, idx))
        for chain in pin_chains.values():
            chain.sort()
            for (_, idx_a), (_, idx_b) in zip(chain, chain[1:]):
                if idx_a == idx_b:
                    continue
                xa, ya = seam_pins[idx_a][:2]
                xb, yb = seam_pins[idx_b][:2]
                dist = math.hypot(xa - xb, ya - yb)
                if dist < 0.5:
                    continue
                key = (idx_a, idx_b) if idx_a < idx_b else (idx_b, idx_a)
                prev = pin_edges.get(key)
                if prev is None or dist < prev:
                    pin_edges[key] = dist
        if pin_edges:
            def _movable(idx: int) -> bool:
                return not (seam_pins[idx][4] and is_hard[idx])
            # Both-immovable edges (runway↔runway: the FAA profile) can
            # never be projected — drop them so they don't block
            # convergence.
            edge_list = [(key, dist) for key, dist in sorted(pin_edges.items())
                         if _movable(key[0]) or _movable(key[1])]
            for _sweep in range(200):
                worst = 0.0
                for (idx_a, idx_b), dist in edge_list:
                    va = pin_vals[idx_a]
                    vb = pin_vals[idx_b]
                    slack = abs(va - vb) - cap * dist
                    if slack <= 1e-4:
                        continue
                    worst = max(worst, slack)
                    move_a = _movable(idx_a)
                    move_b = _movable(idx_b)
                    hi_idx, lo_idx = ((idx_a, idx_b) if va > vb
                                      else (idx_b, idx_a))
                    hi_mov = _movable(hi_idx)
                    lo_mov = _movable(lo_idx)
                    if hi_mov and lo_mov:
                        pin_vals[hi_idx] -= slack / 2.0
                        pin_vals[lo_idx] += slack / 2.0
                    elif hi_mov:
                        pin_vals[hi_idx] -= slack
                    else:
                        pin_vals[lo_idx] += slack
                if worst <= 1e-4:
                    break
            if _os.environ.get("O4_SEAM_DEBUG") == "1":
                n_bad = 0
                for (idx_a, idx_b), dist in edge_list:
                    slack = (abs(pin_vals[idx_a] - pin_vals[idx_b])
                             - cap * dist)
                    if slack > 1e-3:
                        n_bad += 1
                        xa, ya = seam_pins[idx_a][:2]
                        print(f"    [seam-pins] RESIDUAL {slack:.2f} m over "
                              f"{dist:.1f} m at local ({xa:.1f},{ya:.1f})")
                print(f"    [seam-pins] {len(pin_vals)} pin(s), "
                      f"{len(edge_list)} edge(s), final worst residual "
                      f"{worst:.3f} m, {n_bad} edge(s) still over cap")

        # ── Phase 3: write ────────────────────────────────────────────
        for idx, v in pin_vals.items():
            runway = seam_pins[idx][4]
            if runway and is_hard[idx]:
                continue     # runway hard-anchor value already in place
            # Seam wins: override any existing HARD value too.
            elev[idx] = v
            is_hard[idx] = True
            have_initial[idx] = True
        # Publish the pinned indices: seam pins are GRADED-TO hard anchors
        # (user 2026-07-04, "treat the seam like a runway edge or
        # building") — downstream passes that re-stamp or free anchor
        # classes (apron seat-on-spine stamps, the yield pass's
        # movable-pads / free-apron-seats relaxations) must NEVER touch a
        # seam pin: SPLP C-pin was seat-stamped 63.5 → 66.3, then freed
        # from yield_hard, and the final GS parked it 0.7 m above the
        # terrain pin — a bump the law never saw (seam-zone exemption).
        layout._seam_pin_idx = set(pin_vals)  # type: ignore[attr-defined]
        # Lat/lon twin of the pin set for the axes sidecar → the
        # validator flags the SAME vertices (nid space) instead of its
        # legacy 400 m blanket zone.
        layout._seam_pin_ll = [  # type: ignore[attr-defined]
            layout.m_to_ll(seam_pins[i][0], seam_pins[i][1])
            for i in pin_vals]

    # ── Object-bridge deck pins (feature B stage 2, gated) ───────────
    # ``layout._object_bridge_pin_values`` maps vertex-bucket keys →
    # FIXED absolute elevations (deck-end / profile pins written by
    # ``bridges.insert_bridge_deck_end_pins`` /
    # ``insert_bridge_profile_pins`` from ``grade_law`` values; only ever
    # populated under O4_OBJECT_BRIDGE_TERRAIN — absent ⇒ this block is
    # dead and seeding is byte-identical).  Unlike tile-seam pins these
    # are NEVER DEM re-sampled: the pin IS the object's deck elevation
    # (the pavement must meet the rendered deck exactly, spec section
    # 3.2 step 2).  Runs after the seam block so a deck pin coinciding
    # with a seam vertex wins (pavement/deck value beats terrain — the
    # weld ruling's "pavement value always wins").
    bridge_pin_values = getattr(
        layout, "_object_bridge_pin_values", None)
    if bridge_pin_values:
        from ..layout import SHARED_VERTEX_TOL_M as _BRIDGE_TOL
        _bridge_bucket_scale = 1.0 / _BRIDGE_TOL
        _bridge_cps = layout.canonical_points
        bridge_pinned_idx: set = set()
        for s in layout.shapes:
            if s.polygon is None or s.polygon.is_empty:
                continue
            coords = _open_ring(list(s.polygon.exterior.coords))
            if len(coords) < 3:
                continue
            for (x, y) in coords:
                bucket = (int(round(x * _bridge_bucket_scale)),
                          int(round(y * _bridge_bucket_scale)))
                pin_value = bridge_pin_values.get(bucket)
                if pin_value is None:
                    continue
                idx = bucket_to_idx.get(
                    _bridge_cps.get_or_add(float(x), float(y)))
                if idx is None:
                    continue
                elev[idx] = float(pin_value)
                is_hard[idx] = True
                have_initial[idx] = True
                bridge_pinned_idx.add(idx)
        if bridge_pinned_idx:
            # Deck pins share the seam pins' protection: downstream
            # re-stamp / relaxation passes must never move them.
            existing_pin_idx = getattr(layout, "_seam_pin_idx", None)
            layout._seam_pin_idx = (  # type: ignore[attr-defined]
                set(existing_pin_idx) if existing_pin_idx else set()
            ) | bridge_pinned_idx

    # ── Runway-end-skirt HARD PINS (Slice B stage B1, gated) ─────────
    # docs/slice_b_solver_absorption_design.md §B1.  The runway-end skirt
    # is the first terrain feature absorbed into the one-solve graph.  Its
    # rings are built PRE-SOLVE (pipeline, before the solve call) and every
    # ring vertex carries a birth-computed profile value in the shape's
    # ``node_altitudes`` (the inverse-RESA law floor, derived from the
    # already-hard runway profile).  Here each such vertex becomes a HARD
    # PIN at that value — the object-bridge deck-pin pattern (above),
    # mirrored: the pin SOURCE is the shape's own per-vertex
    # ``node_altitudes`` (the skirt carries its values ON the shape, unlike
    # the plates whose values live in ``_object_bridge_pin_values``), so no
    # parallel bucket dict is minted — but the APPLICATION (elev / is_hard /
    # have_initial + the seam-pin protection set) is identical.  The solver
    # grades the neighbouring pavement to MEET these pins and never reshapes
    # them; ``_writeback`` skips ROLE_RUNWAY_CLEARANCE, so the immutable
    # ring keeps its birth values.  Runs after the bridge block so a skirt
    # vertex coinciding with a deck pin yields to the deck (pavement/deck
    # value wins), and after the seam block for the same reason.  GATED:
    # the roles are admitted to the node list only under
    # ``admitted_terrain_roles()`` (master + sub-gate), so with the gate off
    # ``idx`` is never found for a skirt vertex and the block is a no-op —
    # and until B1 moves construction pre-solve no skirt shape is even
    # present at solve time, so the block is doubly inert off-gate.
    _admitted_terrain = admitted_terrain_roles()
    if ROLE_RUNWAY_CLEARANCE in _admitted_terrain:
        _skirt_cps = layout.canonical_points
        skirt_pinned_idx: set = set()
        for s in layout.shapes:
            if (s.role != ROLE_RUNWAY_CLEARANCE
                    or getattr(s, "ref", None) != "runway_end_skirt"):
                continue
            if s.polygon is None or s.polygon.is_empty:
                continue
            na = s.node_altitudes
            if not na:
                continue
            coords = _open_ring(list(s.polygon.exterior.coords))
            if len(coords) < 3:
                continue
            for (x, y), alt in zip(coords, na):
                if alt is None:
                    continue
                idx = bucket_to_idx.get(
                    _skirt_cps.get_or_add(float(x), float(y)))
                if idx is None:
                    continue
                elev[idx] = float(alt)
                is_hard[idx] = True
                have_initial[idx] = True
                skirt_pinned_idx.add(idx)
        if skirt_pinned_idx:
            existing_pin_idx = getattr(layout, "_seam_pin_idx", None)
            layout._seam_pin_idx = (  # type: ignore[attr-defined]
                set(existing_pin_idx) if existing_pin_idx else set()
            ) | skirt_pinned_idx

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
                            s.altitude_high = round(float(_hi9), 2)
                            s.altitude_low = round(float(_lo9), 2)
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
        if s.role == ROLE_BUILDING and _role_grade(ROLE_BUILDING) <= 0.0:
            # Terminal is FLAT (the default: TERMINAL_MAX_GRADE = 0, a terminal
            # sits on one floor altitude — per user 2026-05-18).  The flat
            # equality group already enforced this in the solver; average is just
            # a defensive round.  When TERMINAL_MAX_GRADE > 0 the terminal grades
            # like an apron and falls through to the per-corner branch below.
            avg = sum(corner_elevs) / len(corner_elevs)
            s.altitude = round(float(avg), 2)
            s.altitude_high = None
            s.altitude_low = None
            s.node_altitudes = None
            n_terms += 1
        elif s.role in (ROLE_APRON, ROLE_BUILDING):
            # Per user 2026-05-18: aprons are NOT 100 % flat — they
            # satisfy 1.5 % across their surface, NOT zero gradient.
            # Keep the solver's per-corner altitudes (which it
            # already constrained via all-pair Euclidean edges) so
            # adjacent aprons that share corners don't end up at
            # 4-8 m cliff steps (each apron previously averaged to
            # its own single altitude → adjacent aprons diverged).
            alts = [round(float(e), 2) for e in corner_elevs]
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
                s.altitude_high = round(float(hi), 2)
                s.altitude_low = round(float(lo), 2)
                s.altitude = None
                s.node_altitudes = None
                n_rects += 1
            else:
                alts = [round(float(e), 2) for e in corner_elevs]
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
            alts = [round(float(e), 2) for e in corner_elevs]
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
            alts = [round(float(e), 2) for e in corner_elevs]
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


def _report(icao, n_free, _unused, elapsed,
             n_terms, n_rects, n_juncs):
    # NAME NOTE: "per-surface" survives only as this package's name —
    # the active solve is ONE route-profile solve on the single unified
    # grade graph (route_profile.solve_route_profile).  The old print
    # said "converged in N/N iters" where N was actually the FREE-NODE
    # count, which read as an iteration cap; say what it means.
    import O4_UI_Utils as UI
    UI.vprint(1,
        f"  [pav-builder] {icao}: route-profile solve — "
        f"{n_free} free node(s) in {elapsed:.2f} s; applied to "
        f"{n_terms} terminal/apron(s), {n_rects} rect(s), "
        f"{n_juncs} junction(s).")
