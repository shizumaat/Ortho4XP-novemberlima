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

Today every rule is ISOTROPIC (cL == cT, evaluated against the Euclidean chord
with Δs⊥ = 0), which is exactly the legacy scalar ``cap·dist`` behaviour — so this
module is byte-identical to the prior in-line logic.  The anisotropic form is the
hook for the deliberate junction-law switch (then the readers evaluate
``Allowance.at(Δs∥, Δs⊥)`` instead of collapsing to a scalar).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

# ── Law constants (the adjustable knobs of the law) ──────────────────────────
APRON_ROLE = "apron"

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
    ``cL == cT`` and (with Δs⊥ = 0) is the legacy scalar ``cap·dist``."""
    cL: float
    cT: float

    @classmethod
    def flat(cls, cap: float) -> "Allowance":
        return cls(cap, cap)

    def at(self, ds_parallel: float, ds_perp: float = 0.0) -> float:
        return self.cL * ds_parallel + self.cT * ds_perp

    @property
    def is_flat(self) -> bool:
        return self.cL == self.cT

    def flat_cap(self) -> float:
        """The scalar cap of a flat allowance (legacy ``(a, b, cap)`` form).
        Only valid while the rule is isotropic — asserts so a future anisotropic
        rule can't silently lose its cT through a scalar consumer."""
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

    # CAP selection (first match wins):
    # — a spine pair keeps its route's per-letter taxi cap (looser of the shared
    #   centerlines), the same cap the seater grades that route at.
    if p.spine_caps:
        return Allowance.flat(max(p.spine_caps))
    # — an apron body edge near a taxiway earns the route's blended cap.
    if p.blend_cap_fn is not None:
        return Allowance.flat(p.blend_cap_fn())
    # — otherwise the shape's body cap (apron 1%, junction the taxi cap, …).
    return Allowance.flat(p.body_cap)
