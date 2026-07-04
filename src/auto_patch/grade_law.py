"""THE within-shape grade LAW — the single source of truth for *which* vertex
pairs of a soft airside shape are grade-constrained and *at what budget*.

Both readers consume this one law:
  * the SOLVER, via ``grade_graph.shape_constraints`` (builds a ``PairContext``
    from the in-memory ``GradeShape``/``GradeContext``), and
  * the grade TEST, via ``tools/check_grade`` (builds a ``PairContext`` from the
    emitted OSM)  ← wiring in progress (docs/cleanup_consolidation_plan.md M4).
So the surface we BUILD and the surface we CHECK cannot drift: fix a rule here
once and it is both built and verified.

## The allowance model
A pair's grade budget is anisotropic in the local spine (route) frame:

    allowed |Δz|  =  cL · Δs∥  +  cT · Δs⊥

where ``Δs∥`` is the along-route (spine arc-length) separation and ``Δs⊥`` the
perpendicular offset.  This is what lets a rising CURVE be graded correctly: on
the inside of a turn the edge climbs the same Δz over a shorter physical chord
(so it is steeper per metre) yet is compliant, because its longitudinal budget is
the SPINE arc length it spans, not its own chord — see
``docs/m4_constraint_graph_findings.md`` and the curved-junction model.

The law emits an ``Allowance(cL, cT)`` per pair.  Under the ``O4_ANISO_EDGES``
gate (``docs/anisotropic_edge_handling_plan.md``), ``grade_graph.shape_constraints``
decomposes a spine / junction-body / apron-blend pair against its whole chained
ROUTE and BAKES the anisotropic budget ``cL·Δs∥ + cT·Δs⊥`` (Δs∥ = spine arc) into
the allowance — so a climbing CURVE earns its full arc length and stops being
false-flagged at junctions, and A/B taxiways carry the tighter 2 % transverse cap.
With the gate OFF the allowance is flat (``cL == cT``, Δs⊥ = 0) and reduces to the
legacy scalar ``cap·dist`` — byte-identical to the prior in-line logic.  Either
way every reader evaluates ``Allowance.at(Δs∥, Δs⊥)``; the BAKED allowance returns
its precomputed budget so the solver and validator share one decomposition.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

from .config import (
    APRON_MAX_GRADE, BUILDING_FRONTAGE_MAX_GRADE, BUILDING_FULL_FRONTAGE,
    BUILDING_FULL_FRONTAGE_AREA_M2,
    BUILDING_REACH_CORRIDOR_M, SERVICE_ROAD_MAX_GRADE, TAXI_MAX_GRADE)

# ── Law constants (the adjustable knobs of the law) ──────────────────────────
APRON_ROLE = "apron"

# THE single reach/grade rules, surfaced here so every site refers to ONE value
# and cannot drift into local copies (user 2026-06-29).
#  * ``BUILDING_REACH_CORRIDOR_M`` (imported) — max building↔spine apron reach.
#  * ``APRON_MAX_GRADE`` / ``TAXI_MAX_GRADE`` (imported from config) — the apron
#    (1 %) and taxiway (1.5 %) grade caps; re-exported so reach/seat/spine code
#    stops keeping local ``_APRON_CAP`` / ``_ENTRY_CAP`` copies.
#  * runway-CONTACT geometry: a taxi centerline endpoint within
#    ``RUNWAY_CONTACT_M`` of a runway is a contact; the nearest emitted node
#    within ``RUNWAY_JOIN_NEAR_M`` of it is the anchored join node.  One source
#    for ``grade_graph._runway_anchors``, the validator's runway-join check, and
#    ``lateral_spine_nodes`` (was three copies of 12 m / 18 m).
RUNWAY_CONTACT_M = 12.0
RUNWAY_JOIN_NEAR_M = 18.0


def runway_join_contact(ln, endpoint, rwy_polygon):
    """THE runway-join contact point for a taxi centerline endpoint (single source
    for ``grade_graph._runway_anchors`` AND the validator's runway-join check, so
    the solver anchors exactly the node the validator checks).

    Returns the ``(x, y)`` where the taxiway↔runway CONTACT node sits, or ``None``
    when ``endpoint`` is not within ``RUNWAY_CONTACT_M`` of the runway.

    A taxi route connects to the runway CENTERLINE, so when the endpoint lies
    INSIDE the runway the real contact is where the centerline crosses the runway
    EDGE — that is where the emitted taxi/junction/runway node is welded, and it
    is what ``RUNWAY_JOIN_NEAR_M`` must reach.  On a WIDE runway the centerline is
    ~half the width from the edge (HECA shoulder-widened to 86 m ⇒ ~43 m ≫ the
    18 m join radius), so anchoring at the deep-interior endpoint finds no emitted
    node and the join is silently missed → the taxiway grades to DEM off the runway
    (a big drop at F→05R, T5→05C).  Using the edge crossing fixes both.  For an
    endpoint at/outside the edge the endpoint is already the contact."""
    from shapely.geometry import Point
    P = Point(endpoint)
    if rwy_polygon.distance(P) > RUNWAY_CONTACT_M:
        return None
    if not rwy_polygon.covers(P):
        return (endpoint[0], endpoint[1])
    try:
        xing = ln.intersection(rwy_polygon.boundary)
    except Exception:
        return (endpoint[0], endpoint[1])
    pts = ([xing] if getattr(xing, "geom_type", "") == "Point"
           else [g for g in getattr(xing, "geoms", [])
                 if g.geom_type == "Point"])
    if not pts:
        return (endpoint[0], endpoint[1])
    ex, ey = endpoint
    best = min(pts, key=lambda p: (p.x - ex) ** 2 + (p.y - ey) ** 2)
    return (best.x, best.y)


def building_requires_full_frontage(area_m2: float) -> bool:
    """THE canonical building-size reach rule (single source for seater AND
    checker).  A building at/above ``BUILDING_FULL_FRONTAGE_AREA_M2`` must have
    its ENTIRE apron-facing frontage reachable from the taxi route within grade
    (a terminal maneuvers along its whole face).  A SMALLER building need only
    reach the spine at its central chord — it is seated at that level and acts as
    a LOCAL reach ANCHOR: its non-central frontage and the apron stepping up to
    it within the apron cap grade FROM the pad, not from the runway route, so
    those points are not runway-reach-constrained.  Honours the
    ``BUILDING_FULL_FRONTAGE`` gate (off ⇒ all buildings use the central-chord
    rule, the pre-2026-06-27 model).

    Consumed by ``route_profile.anchors.build_building_seats`` (which frontage to
    seat at) and ``grade_graph_validate.route_band_violations`` (small pads are
    local anchors; large frontages stay route-reach-checked) — so the level we
    BUILD a building at and the reach we CHECK it against come from one rule."""
    return bool(BUILDING_FULL_FRONTAGE) and area_m2 >= BUILDING_FULL_FRONTAGE_AREA_M2

# Pairs closer than this are ring/relative noise — not a grade constraint.
MIN_PAIR_DIST_M = 0.5

# Max length of an APRON interior body↔body grade chord: beyond this a chord
# across a wide apron is not a real grade path (each point grades to its DIRECT
# spine, not to a far interior point), so it is dropped to decouple the building
# frontages from the route-maxed-low far interior.  Ring-adjacent, spine,
# building-frontage and seam chords are NEVER dropped by this.  0 = unlimited.
APRON_BODY_CHORD_MAX_M = float(os.environ.get("O4_APRON_BODY_CHORD_MAX_M", "60"))


@dataclass(frozen=True)
class Allowance:
    """Max |Δz| budget for a pair: ``cL·Δs∥ + cT·Δs⊥``.  A flat allowance has
    ``cL == cT`` and (with Δs⊥ = 0) is the legacy scalar ``cap·dist``.

    When the pair has been decomposed against its route up front (anisotropic
    edges, ``grade_graph.shape_constraints``), the resulting scalar budget is
    BAKED into ``budget``: ``at()`` then returns it directly, ignoring the
    distance a consumer passes.  This is what lets every consumer keep its
    existing ``cap.at(d, 0.0)`` call yet receive the route-arc budget — the
    decomposition is computed ONCE in the law (no per-site copy, so the solver and
    validator graphs can't drift).  ``budget is None`` ⇒ a plain live allowance."""
    cL: float
    cT: float
    budget: Optional[float] = None

    @classmethod
    def flat(cls, cap: float) -> "Allowance":
        return cls(cap, cap)

    @classmethod
    def baked(cls, cL: float, cT: float, budget: float) -> "Allowance":
        """An allowance whose anisotropic budget is already evaluated (against the
        pair's route).  ``at()`` returns ``budget``; ``flat_cap()`` still reports
        the longitudinal ``cL`` for %-cap messages."""
        return cls(cL, cT, budget)

    def at(self, ds_parallel: float, ds_perp: float = 0.0) -> float:
        if self.budget is not None:
            return self.budget
        # L2 (ellipse) composition: a surface with principal gradient limits
        # (cL, cT) allows |Δz| = √((cL·Δs∥)² + (cT·Δs⊥)²) in an oblique
        # direction.  The old L1 sum over-allowed diagonals by up to √2 —
        # measured: 4 % road-carve pairs read LEGAL at 5.6 % (user-visible
        # steep edges at zero reported violations, 2026-07-03).
        a = self.cL * ds_parallel
        b = self.cT * ds_perp
        return (a * a + b * b) ** 0.5

    @property
    def is_flat(self) -> bool:
        return self.cL == self.cT

    def flat_cap(self) -> float:
        """The longitudinal scalar cap.  For a flat LIVE allowance this is the
        legacy ``(a, b, cap)`` value; for a BAKED allowance it is ``cL`` (the
        %-cap to report).  Asserts only for a live anisotropic allowance — that
        would silently lose its ``cT`` through a scalar consumer."""
        if self.budget is None:
            assert self.is_flat, "anisotropic allowance has no single scalar cap"
        return self.cL


@dataclass(frozen=True)
class PairContext:
    """Everything the law needs about ONE vertex pair, computed by the reader
    from its own representation (in-memory shape, or emitted OSM).

    The two EXPENSIVE geometry predicates are injected as thunks so the law can
    evaluate them lazily (only for pairs that survive the cheap skips), matching
    the legacy in-line short-circuiting — the reader supplies *how* to test them
    from its representation, the law decides *when*:

    ``visible_fn``       returns whether the chord stays inside the pavement;
                         None ⇒ no visibility constraint (always visible).
    ``crosses_spine_fn`` returns whether the chord crosses a spine the shape owns
                         (the climb is via the spine, not this diagonal); the
                         reader sets it to None unless the pair is non-spine and
                         non-ring-adjacent (where the rule can apply).
    ``blend_cap_fn``     lazy apron↔taxi blend cap (evaluated ONLY for a surviving
                         non-spine apron pair), or None.
    ``spine_caps``       caps of the centerline(s) BOTH endpoints lie on; () ⇒ not
                         a spine pair (the climb is carried by this pair directly).
    """
    role: str
    dist: float
    ring_adjacent: bool
    a_seam: bool
    b_seam: bool
    a_building: bool
    b_building: bool
    spine_caps: tuple
    body_cap: float
    visible_fn: Optional[Callable[[], bool]] = None
    crosses_spine_fn: Optional[Callable[[], bool]] = None
    blend_cap_fn: Optional[Callable[[], float]] = None
    # ``both_road``: both endpoints sit on a service-road carve through the host
    # (so the pair descends at the ROAD cap, not the host body cap).
    both_road: bool = False


SKIP: Optional[Allowance] = None


def classify_pair(p: PairContext) -> Optional[Allowance]:
    """Apply the within-shape grade law to one pair.  Returns the pair's
    ``Allowance``, or ``SKIP`` (None) if the pair is not a regulated grade path.

    Rules in precedence order (first match wins).  ELIGIBILITY (skip) rules:
    """
    # — a seam endpoint is DEM-controlled, not solver-controlled.
    if p.a_seam or p.b_seam:
        return SKIP
    # — both ends on building pads ⇒ inter-pad frontage = an allowed building
    #   ↔building step, not an apron grade path.
    if p.a_building and p.b_building:
        return SKIP
    # — sub-noise separation is not a grade constraint.
    if p.dist < MIN_PAIR_DIST_M:
        return SKIP
    # — a non-adjacent chord that leaves the pavement is not a surface path.
    if not p.ring_adjacent and p.visible_fn is not None and not p.visible_fn():
        return SKIP
    # — the climb between the two sides is carried by the SPINE at the taxi cap;
    #   the straight diagonal across it is not an independent grade path.
    if p.crosses_spine_fn is not None and p.crosses_spine_fn():
        return SKIP
    # — a long apron body↔body chord grades to its spine, not to a far interior
    #   point (decouples building frontages from the route-maxed-low interior).
    if (p.role == APRON_ROLE and APRON_BODY_CHORD_MAX_M
            and not p.spine_caps and not p.ring_adjacent
            and not p.a_building and not p.b_building
            and p.dist > APRON_BODY_CHORD_MAX_M):
        return SKIP

    # CAP selection — base cap (first match wins):
    # — a spine pair keeps its route's per-letter taxi cap (looser of the shared
    #   centerlines), the same cap the seater grades that route at.
    if p.spine_caps:
        cap = max(p.spine_caps)
    # — an apron body edge near a taxiway earns the route's blended cap.
    #   NEVER for a pair touching a BUILDING pad: the building↔spine 1 %
    #   rule is the binding constraint (user 2026-07-02) — blending it to
    #   the route cap (or a 4 % service route) silently legalised a 3.5 %
    #   frontage chord at SPJC building-10031.
    elif (p.blend_cap_fn is not None
          and not p.a_building and not p.b_building):
        cap = p.blend_cap_fn()
    # — otherwise the shape's body cap (apron 1%, junction the taxi cap, …).
    else:
        cap = p.body_cap

    # BUILDINGS ARE THE HEAVIEST CONSTRAINT (user 2026-07-02/03): a pair
    # touching a building pad is the frontage 1 % rule regardless of the
    # HOST face's role.  The blend / road-carve relaxations above already
    # exclude building pairs, but a frontage chord inside a
    # ``service_junction`` face (service roads hug terminals) never took
    # those branches — it inherited the host's 4 % BODY cap and legalised
    # the >1 % terminal-side ramps the user sees in the sim (SPJC: 15
    # frontage pairs up to 3.8 % read legal at "cap 4.0%").
    if (p.a_building or p.b_building) and cap > BUILDING_FRONTAGE_MAX_GRADE:
        cap = BUILDING_FRONTAGE_MAX_GRADE

    # RELAXATIONS — a feature CARVED INTO the host that legitimately grades
    # steeper than the host body.  Applied by BOTH readers (the solver builds to
    # it, the validator confirms it) — never a test-only fudge: the carve corners
    # lie ON the host ring, so without this the host law would wrongly regulate
    # the carved feature's own descent.  Relax only (raise the cap).
    # — both endpoints on a service-road carve → the road's cap.  NEVER for a
    #   pair touching a BUILDING pad: service roads hug terminal frontages, so
    #   the road zone otherwise swallows the building↔spine 1 % rule (SPJC
    #   building-10031: a 3.5 % frontage chord read as a legal 4 % road pair —
    #   user 2026-07-02, buildings are the heaviest constraint).
    if (p.both_road and SERVICE_ROAD_MAX_GRADE > cap
            and not p.a_building and not p.b_building):
        cap = SERVICE_ROAD_MAX_GRADE

    return Allowance.flat(cap)
