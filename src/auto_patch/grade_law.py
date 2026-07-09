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
    ADJACENT_GROUND_LIP_MAX_DOWN_SLOPE, ADJACENT_GROUND_LIP_MIN_DOWN_SLOPE,
    ADJACENT_GROUND_LIP_WIDTH_M, ADJACENT_GROUND_UNGRADED_STRIP_MAX_UP_SLOPE,
    APRON_MAX_GRADE, APRON_SHOULDER_MAX_DOWN_SLOPE,
    APRON_SHOULDER_MIN_DOWN_SLOPE, APRON_SHOULDER_WIDTH_M,
    BUILDING_FRONTAGE_MAX_GRADE, BUILDING_FULL_FRONTAGE,
    BUILDING_FULL_FRONTAGE_AREA_M2,
    BUILDING_REACH_CORRIDOR_M, CLEARANCE_LATERAL_MAX_SLOPE,
    CLEARANCE_MAX_REACH_M, JUNCTION_MESH_CONSTRAINTS,
    RUNWAY_END_CLEARANCE_LENGTH_BY_CODE, RUNWAY_STRIP_BAND_MIN_DOWN_SLOPE,
    RUNWAY_STRIP_BAND_MAX_DOWN_SLOPE_BY_CODE, RUNWAY_STRIP_HALF_WIDTH_BY_CODE,
    SERVICE_ROAD_MAX_GRADE, TAXI_MAX_GRADE, TAXIWAY_STRIP_BAND_MAX_DOWN_SLOPE,
    TAXIWAY_STRIP_BAND_MIN_DOWN_SLOPE, runway_code_number,
    taxiway_strip_graded_half_width_for_letter)

# ── Law constants (the adjustable knobs of the law) ──────────────────────────
APRON_ROLE = "apron"
# The junction-family roles the JUNCTION MESH rule in ``classify_pair`` applies
# to.  Defined HERE (the law) and re-exported by ``grade_graph`` so the law and
# its readers share one definition.
JUNCTION_ROLES = ("junction", "service_junction")

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

# ── Runway end skirt law (inverse RESA — downward terrain governance) ────────
# Terrain beyond a runway end may not DROP away arbitrarily: FAA AC
# 150/5300-13B §3.16.5 caps the RSA longitudinal grade at 0…−3 % for the
# first 200 ft (61 m) beyond the end and −5 % beyond, with grade changes
# limited to ±2 % per 100 ft (30.5 m); ICAO Annex 14 §4.7 caps RESA
# downward slopes at 5 %.  Beyond the governed footprint a drop is LAWFUL
# (Madeira-style), so the governed length is also the hard cap on emitted
# fill.  Single source for the Pass D skirt EMITTER
# (``clearance._emit_resa_skirt``) and the ``check_grade`` validator —
# regulatory basis and plan: ``docs/runway_end_skirt_plan.md``.
RUNWAY_END_SKIRT_NEAR_ZONE_M = 61.0             # FAA "first 200 feet"
RUNWAY_END_SKIRT_NEAR_MAX_DOWN_GRADE = 0.03     # 0…−3 % in the near zone
RUNWAY_END_SKIRT_MAX_DOWN_GRADE = 0.05          # −5 % beyond
RUNWAY_END_SKIRT_MAX_GRADE_CHANGE_PER_M = 0.02 / 30.5   # ±2 % per 100 ft

# Governed-length scaling by approach class (per-end, from
# ``config.runway_end_approach_class``).  Visual ends clamp to the ICAO
# 90 m minimum; precision ends extend to the FAA C/D/E footprint
# (1,000 ft ≈ 305 m for code 3/4, the 240 m ICAO recommendation for
# smaller precision runways).  Non-precision uses the by-code base.
RUNWAY_END_SKIRT_VISUAL_MAX_LENGTH_M = 90.0
RUNWAY_END_SKIRT_PRECISION_MIN_LENGTH_M = 240.0
RUNWAY_END_SKIRT_PRECISION_CODE34_LENGTH_M = 305.0


def runway_end_governed_length_m(
        runway_length_m: float, approach_class: str) -> float:
    """THE distance beyond the pavement end within which the down-slope
    floor applies (and beyond which a drop is lawful).  Base footprint by
    ICAO code number (``RUNWAY_END_CLEARANCE_LENGTH_BY_CODE``), scaled by
    the end's approach class — better approaches earn a longer governed
    apron of terrain, per FAA AC 150/5300-13B Appendix G."""
    code = runway_code_number(runway_length_m)
    base = RUNWAY_END_CLEARANCE_LENGTH_BY_CODE[code]
    if approach_class == "visual":
        return min(base, RUNWAY_END_SKIRT_VISUAL_MAX_LENGTH_M)
    if approach_class == "precision":
        if code >= 3:
            return max(base, RUNWAY_END_SKIRT_PRECISION_CODE34_LENGTH_M)
        return max(base, RUNWAY_END_SKIRT_PRECISION_MIN_LENGTH_M)
    return base


# Stop the skirt this far short of a constraining feature (road /
# water) so the feature keeps its own approach embankment.
RUNWAY_END_SKIRT_CONSTRAINT_MARGIN_M = 5.0


def runway_end_constrained_length_m(
        governed_length_m: float,
        constraint_distance_m: float | None) -> float:
    """Clamp the governed length when real infrastructure crosses the
    end zone.  No reliable EMAS data source exists (user ruling
    2026-07-05), but a road, service road or water body close beyond a
    runway end IS the fingerprint of a non-standard end — the real
    world did not build a full-length RSA there (EMAS / declared
    distances instead, e.g. KCLT 18L: perimeter road at the blast-pad
    end).  The skirt ends a margin short of the first constraint; with
    the constraint at the pavement end the skirt vanishes entirely."""
    if constraint_distance_m is None:
        return governed_length_m
    return max(0.0, min(
        governed_length_m,
        constraint_distance_m - RUNWAY_END_SKIRT_CONSTRAINT_MARGIN_M))


def _runway_end_skirt_signed_grade(
        distance_m: float, start_grade: float) -> float:
    """Signed grade (positive = climbing) of the LOWEST lawful surface at
    ``distance_m`` beyond the runway end.  The steepest permissible
    descent is bounded by BOTH what the grade-change rate can reach from
    the runway's own end grade AND the zone's down-grade cap; the cap
    itself eases from −3 % to −5 % at the near-zone boundary under the
    same rate limit, so the floor has no curvature kink anywhere."""
    rate = RUNWAY_END_SKIRT_MAX_GRADE_CHANGE_PER_M
    reachable = start_grade - rate * distance_m
    if distance_m <= RUNWAY_END_SKIRT_NEAR_ZONE_M:
        lawful = -RUNWAY_END_SKIRT_NEAR_MAX_DOWN_GRADE
    else:
        lawful = max(
            -RUNWAY_END_SKIRT_MAX_DOWN_GRADE,
            -RUNWAY_END_SKIRT_NEAR_MAX_DOWN_GRADE
            - rate * (distance_m - RUNWAY_END_SKIRT_NEAR_ZONE_M))
    return max(lawful, reachable)


def runway_end_skirt_profile_breakpoints(
        start_grade: float = 0.0) -> list[float]:
    """Distances (m, ascending) where the floor profile's GRADE LAW
    changes — the boundaries of its piecewise-linear-grade segments.
    Between consecutive breakpoints the floor is a single quadratic, so
    an emitter rendering it as ruled bands split at these breakpoints
    bounds the chord-vs-floor sagitta at ``rate · L² / 8`` (≤ 0.31 m for
    the 61 m near zone) — far inside the fill trigger.  Single source
    for the Pass D band edges AND the floor integration below."""
    start_grade = min(0.0, start_grade)
    rate = RUNWAY_END_SKIRT_MAX_GRADE_CHANGE_PER_M
    return sorted({
        RUNWAY_END_SKIRT_NEAR_ZONE_M,
        RUNWAY_END_SKIRT_NEAR_ZONE_M
        + (RUNWAY_END_SKIRT_MAX_DOWN_GRADE
           - RUNWAY_END_SKIRT_NEAR_MAX_DOWN_GRADE) / rate,
        (start_grade + RUNWAY_END_SKIRT_NEAR_MAX_DOWN_GRADE) / rate,
        (start_grade + RUNWAY_END_SKIRT_MAX_DOWN_GRADE) / rate,
    })


def runway_end_skirt_floor_profile(
        distances_m: list[float], start_grade: float = 0.0) -> list[float]:
    """THE lowest lawful surface beyond a runway end, as DEPTHS (m, ≥ 0)
    below the runway-end elevation at each requested distance.

    The floor starts at the runway's own end grade (``start_grade``,
    signed, positive = the runway climbs toward its end) so a DESCENDING
    runway carries no grade discontinuity into the skirt, then steepens
    under the grade-change rate limit to the near-zone cap (−3 %) and the
    far cap (−5 %).  A CLIMBING end grade clamps to 0 at the pavement
    end: the FAA near zone permits only downward slopes ("between 0 and
    3.0 percent, with any slope being downward from the ends"), so a
    crest legally terminates AT the runway end — and the skirt is
    FILL-only; terrain above the pavement-end elevation is Pass C's
    (cut) domain.

    The grade function is piecewise linear, so trapezoid integration
    between its breakpoints is EXACT — the emitter and the validator
    evaluate identical floors.
    """
    start_grade = min(0.0, start_grade)
    # Breakpoints of the piecewise-linear signed-grade function: the
    # near-zone boundary, the cap's own −3 %→−5 % easing end, and where
    # the curvature-reachable line meets each cap level.
    breakpoints = runway_end_skirt_profile_breakpoints(start_grade)

    def _depth(distance_m: float) -> float:
        drop = 0.0
        previous = 0.0
        for cut in [b for b in breakpoints if 0.0 < b < distance_m] \
                + [distance_m]:
            segment = cut - previous
            drop -= 0.5 * segment * (
                _runway_end_skirt_signed_grade(previous, start_grade)
                + _runway_end_skirt_signed_grade(cut, start_grade))
            previous = cut
        return max(0.0, drop)

    return [_depth(d) for d in distances_m]


def runway_end_governed_length_beyond_pavement_m(
        governed_length_m: float, pavement_beyond_end_m: float) -> float:
    """Governed length REMAINING beyond the overrun-pavement exit.

    The FAA runway safety area is measured from the RUNWAY END, and any
    blast pad / stopway pavement past the end sits INSIDE it (AC
    150/5300-13B §3.16 — the safety area encompasses the stopway), so
    overrun pavement CONSUMES the first ``pavement_beyond_end_m`` of the
    governed footprint.  Before 2026-07-09 the emitter applied the full
    governed length from the pavement exit instead, extending every
    skirt by its blast-pad length (KCLT 18R: 124 m pad → fill to 429 m
    past the end vs the lawful 305 m; user report 'about 70 m too long'
    = the 59-71 m HECA pads).  Returns 0 when pavement covers the whole
    footprint (the skirt vanishes; the KCLT-18L EMAS-end class)."""
    return max(0.0, governed_length_m - max(0.0, pavement_beyond_end_m))


def runway_end_skirt_profile_breakpoints_beyond_pavement(
        start_grade: float = 0.0,
        pavement_beyond_end_m: float = 0.0) -> list[float]:
    """``runway_end_skirt_profile_breakpoints`` re-expressed as distances
    beyond the PAVEMENT EXIT when that exit sits ``pavement_beyond_end_m``
    past the runway end: the law profile is anchored at the runway end,
    so its grade-law breakpoints shift inward by the overrun length
    (breakpoints the pavement already consumed drop out)."""
    advance = max(0.0, pavement_beyond_end_m)
    return sorted({
        b - advance
        for b in runway_end_skirt_profile_breakpoints(start_grade)
        if b > advance + 1e-9})


def runway_end_skirt_floor_profile_beyond_pavement(
        distances_m: list[float], start_grade: float = 0.0,
        pavement_beyond_end_m: float = 0.0) -> list[float]:
    """Floor DEPTHS (m, ≥ 0) below the pavement-EXIT elevation at each
    distance beyond the exit, for an exit ``pavement_beyond_end_m`` past
    the runway end.

    The descent law is anchored at the RUNWAY END (see
    ``runway_end_governed_length_beyond_pavement_m``), so by the exit the
    profile is already ``pavement_beyond_end_m`` into its descent — the
    fill starts FLUSH at the exit-edge elevation (the overrun pavement
    carries its own solved profile) but falls at the ADVANCED profile's
    grade immediately, instead of restarting the gentle 0→−3 % easing a
    second time.  With no overrun pavement this IS
    ``runway_end_skirt_floor_profile``."""
    advance = max(0.0, pavement_beyond_end_m)
    if advance <= 0.0:
        return runway_end_skirt_floor_profile(distances_m, start_grade)
    depths = runway_end_skirt_floor_profile(
        [advance] + [advance + d for d in distances_m], start_grade)
    return [d - depths[0] for d in depths[1:]]


# ── Adjacent-ground LATERAL grade law (Fable 2026-07-08) ─────────────────────
# The lateral generalization of the runway-END skirt: ground beside a paved
# surface is a two-zone-plus-ungraded CORRIDOR off the pavement EDGE.  The
# regulatory model, the four Noah rulings and the slice plan live in
# docs/adjacent_ground_grade_law_plan.md; the rule VALUES live in config.py
# (single source).  Only the zone MATH — accumulated so the corridor bounds are
# CONTINUOUS functions of the distance d — lives here.
#
# Ruling 1 (ENFORCE FULLY): each graded zone is a mandatory-DOWN band with
# DIRECTION, so a FLAT surface (offset 0) is OUTSIDE the corridor within zones
# 1-2 (its ceiling is strictly below 0).  This is what lets the emitter regrade
# flat surrounds to the lawful drainage slope, and it is the boundary-bridge
# killer: zone-3's floor is UNBOUNDED down, so a cliff beyond the graded band
# renders as DEM (never force-filled).

# Role → strip FAMILY.  Runway ENDS are NOT a family here (the skirt law owns
# them); "runway"/"runway_crossing" mean the LATERAL runway strip.
_ADJACENT_RUNWAY_ROLES = frozenset({"runway", "runway_crossing"})
_ADJACENT_APRON_ROLES = frozenset({"apron", "stand", "terminal"})
_ADJACENT_SERVICE_ROLES = frozenset({"service_road", "service_junction"})
_ADJACENT_TAXIWAY_ROLES = frozenset({
    "taxiway", "primary_parallel", "secondary_parallel", "stub",
    "cross_connector", "junction",
})


def _adjacent_strip_envelope(
        graded_half_width_m: float, band_min_down: float,
        band_max_down: float, reach_m: float,
        distance_m: float) -> tuple[Optional[float], Optional[float]]:
    """The shared runway/taxiway two-zone-plus-ungraded corridor, given the
    family's graded WIDTH, its zone-2 min/max DOWN slopes and its outward reach.

    Returns ``(floor_offset, ceiling_offset)`` in metres relative to the
    pavement-edge elevation (positive = above the edge).  The bounds ACCUMULATE
    across zone boundaries so they are continuous in ``distance_m``:

      * Zone 1 (0 .. lip): mandatory-down lip 3-5 %.
          ceiling = -lip_min_down · d ,  floor = -lip_max_down · d
      * Zone 2 (lip .. W): mandatory-down graded band, continuing from the lip's
        endpoint values (NOT restarted at 0):
          ceiling = ceiling(lip) - band_min_down · (d - lip)
          floor   = floor(lip)   - band_max_down · (d - lip)
      * Zone 3 (W .. reach): ungraded strip — ceiling continues UP at ≤5 % from
        the band's endpoint ceiling, floor = None (cliffs lawful).
      * d ≥ reach: (None, None) — ungoverned (OLS territory / earthwork bound).
    """
    lip = ADJACENT_GROUND_LIP_WIDTH_M
    lip_min = ADJACENT_GROUND_LIP_MIN_DOWN_SLOPE
    lip_max = ADJACENT_GROUND_LIP_MAX_DOWN_SLOPE
    if distance_m <= 0.0:
        return (0.0, 0.0)                       # flush at the edge
    if distance_m >= reach_m:
        return (None, None)
    if distance_m <= lip:                       # ZONE 1 — drainage lip
        return (-lip_max * distance_m, -lip_min * distance_m)
    lip_ceiling = -lip_min * lip
    lip_floor = -lip_max * lip
    if distance_m <= graded_half_width_m:       # ZONE 2 — graded band
        ceiling = lip_ceiling - band_min_down * (distance_m - lip)
        floor = lip_floor - band_max_down * (distance_m - lip)
        return (floor, ceiling)
    band_ceiling = lip_ceiling - band_min_down * (graded_half_width_m - lip)
    up = ADJACENT_GROUND_UNGRADED_STRIP_MAX_UP_SLOPE
    ceiling = band_ceiling + up * (distance_m - graded_half_width_m)  # ZONE 3
    return (None, ceiling)


def adjacent_ground_envelope(
        role: str, code_number: Optional[int], code_letter: Optional[str],
        distance_from_pavement_edge_m: float,
) -> tuple[Optional[float], Optional[float]]:
    """THE lawful corridor for ground adjacent to a paved surface, as a signed
    ``(floor_offset_m, ceiling_offset_m)`` relative to the pavement-EDGE
    elevation (positive = above the edge), at lateral distance
    ``distance_from_pavement_edge_m`` (``d``) out from the edge.

    A terrain point is lawful iff ``floor_offset ≤ (point − edge) ≤ ceiling``.
    ``None`` for a bound means UNBOUNDED in that direction: a ``None`` ceiling
    permits any rise (never cut here); a ``None`` floor permits any drop (never
    filled — a cliff is lawful).

    The corridor is the two-zone-plus-ungraded profile of
    docs/adjacent_ground_grade_law_plan.md, ENFORCED FULLY (ruling 1) as
    mandatory-DOWN graded bands, so within zones 1-2 the ceiling is strictly
    below 0 and a FLAT surround (offset 0) is OUTSIDE the corridor — the emitter
    regrades it to the lawful drainage slope.  All bounds ACCUMULATE across zone
    boundaries, so both are CONTINUOUS functions of ``d`` (no step at the lip
    edge or the band edge; the floor's finite→None transition at the band edge
    only OPENS the corridor downward).  Pure, deterministic, no geometry deps.

    Roles:
      * runway / runway_crossing — LATERAL runway strip.  Keyed by ICAO code
        NUMBER (ruling 2): graded WIDTH = ``RUNWAY_STRIP_HALF_WIDTH_BY_CODE``;
        band down-cap 3 % (code 3/4 ≈ AAC C-E) / 5 % (code 1/2 ≈ AAC A/B),
        min 1.5 % (FAA RSA minimum).  ``code_letter`` is ignored.
      * taxiway family (taxiway, parallels, stub, cross_connector, junction) —
        taxiway strip.  Keyed by ICAO code LETTER (ruling 2): graded WIDTH =
        OMGWS table (``taxiway_strip_graded_half_width_for_letter``); band down
        1.5-5 %.  ``code_number`` is ignored.
      * apron family (apron, stand, terminal) — a 3 m FAA-recommended shoulder
        (1-3 % down), then zone-3 semantics immediately (ceiling ≤5 % up, floor
        free).  Both code args ignored.  The retaining-wall face for a deep drop
        (``APRON_EDGE_WALL_MIN_DROP_M``) is the emitter's job (slice 3).
      * service_road / service_junction — UNCHANGED 15 m cut-only flat shadow
        (ceiling 0 out to ``CLEARANCE_MAX_REACH_M["service"]``, floor free): a
        conservative design choice EXCEEDING the AASHTO 2-3 m low-speed clear
        zone (documented in docs/STANDARDS.md), not a regulatory mandate.

    Runway ENDS are explicitly OUT OF SCOPE: the longitudinal runway-end skirt
    law (``runway_end_skirt_floor_profile`` / ``runway_end_governed_length_m``)
    owns terrain beyond a runway end.  This function is the LATERAL law only.

    Raises ``ValueError`` for an unrecognised role (a law must not silently pick
    a corridor for a surface it does not model).
    """
    d = distance_from_pavement_edge_m
    if role in _ADJACENT_RUNWAY_ROLES:
        if code_number is None:
            raise ValueError("runway adjacent-ground envelope needs code_number")
        return _adjacent_strip_envelope(
            RUNWAY_STRIP_HALF_WIDTH_BY_CODE[code_number],
            RUNWAY_STRIP_BAND_MIN_DOWN_SLOPE,
            RUNWAY_STRIP_BAND_MAX_DOWN_SLOPE_BY_CODE[code_number],
            CLEARANCE_MAX_REACH_M["runway"], d)
    if role in _ADJACENT_TAXIWAY_ROLES:
        return _adjacent_strip_envelope(
            taxiway_strip_graded_half_width_for_letter(code_letter),
            TAXIWAY_STRIP_BAND_MIN_DOWN_SLOPE,
            TAXIWAY_STRIP_BAND_MAX_DOWN_SLOPE,
            CLEARANCE_MAX_REACH_M["taxiway"], d)
    if role in _ADJACENT_APRON_ROLES:
        # Aprons ride the maneuvering-network reach (taxiway); the only governed
        # band is the 3 m shoulder, then zone-3 semantics immediately.
        reach = CLEARANCE_MAX_REACH_M["taxiway"]
        if d <= 0.0:
            return (0.0, 0.0)
        if d >= reach:
            return (None, None)
        if d <= APRON_SHOULDER_WIDTH_M:
            return (-APRON_SHOULDER_MAX_DOWN_SLOPE * d,
                    -APRON_SHOULDER_MIN_DOWN_SLOPE * d)
        shoulder_ceiling = -APRON_SHOULDER_MIN_DOWN_SLOPE * APRON_SHOULDER_WIDTH_M
        up = ADJACENT_GROUND_UNGRADED_STRIP_MAX_UP_SLOPE
        return (None, shoulder_ceiling + up * (d - APRON_SHOULDER_WIDTH_M))
    if role in _ADJACENT_SERVICE_ROLES:
        # UNCHANGED cut-only flat shadow: cut anything above the edge within the
        # 15 m band, never fill (floor free).  CLEARANCE_LATERAL_MAX_SLOPE == 0
        # ⇒ the ceiling stays at the edge level across the whole band.
        if d >= CLEARANCE_MAX_REACH_M["service"]:
            return (None, None)
        return (None, CLEARANCE_LATERAL_MAX_SLOPE * d)
    raise ValueError(f"adjacent_ground_envelope: unmodelled role {role!r}")


# ── Spine crown offset (user 2026-07-07, part 30) ────────────────────────────
# Crowned pavement is a DESIGNED sub-cap offset: every node carries a crown
# drop c ≥ 0 (``crown.build_crown_drop_field`` — the ONE field; runways get a
# uniform per-piece drop stamped at profile evaluation), and a pair's grade
# budget re-centres on the crown target:
#
#     |Δz − crown_pair_offset(c_a, c_b)| ≤ Allowance.at(Δs∥, Δs⊥)
#
# Both readers evaluate this with the SAME field: the SOLVER by running in
# uncrowned space z′ = z + c (its writeback subtracts c — mathematically
# identical to offset edges, and single-valued per canonical node so welds
# can never tear), the VALIDATOR by reading the field from the axes sidecar
# (``crown_drops``) / ``layout._crown_drop_key`` and re-centring here.  Since
# every crown rate ≤ every transverse cap (1 % ≤ 1.5 %, service 1.5 % ≤ 2 %),
# the re-centred band always still contains the FLAT surface — the offset can
# only restore budget the crown consumed, never flag an uncrowned patch.
def crown_pair_offset(drop_a: float, drop_b: float) -> float:
    """THE crown target of ``z_a − z_b`` for a pair whose endpoints carry
    crown drops ``drop_a`` / ``drop_b`` (0 when unknown/uncrowned)."""
    return (drop_b or 0.0) - (drop_a or 0.0)


# ── Runway within-shape LATERAL scoping (user 2026-07-08) ────────────────────
# A de-segmented runway (``O4_RUNWAY_SINGLE_POLY``, default on) emits ONE polygon
# ring per ref whose FAA profile stations live as interior LONG-EDGE vertices.
# The within-shape all-pair grade check on that ring conflates two DISTINCT laws:
#   * LATERAL — the within-shape check's real domain (cross-section crown / edge
#     roll), measured by SAME-station and ADJACENT-station pairs; and
#   * LONGITUDINAL — owned by the FAA profile law (``check_runway_profile``,
#     ring-aware since ff332e9, + the spine-profile check), measured by a pair
#     spanning 2+ station intervals.
# A multi-station chord IS an at-cap longitudinal grade the profile law already
# governs; counting it in the within-shape all-pair domain double-books it
# against a check with no jurisdiction there (SPLP 02/20: ≤+0.11 % at-cap chords
# spanning stations pushed within 18→31).  So a runway-ring vertex pair is IN the
# within-shape domain iff its endpoints are same- or adjacent-station:
# ``|station_index_a − station_index_b| <= 1``.  This extends the part-30i
# crown-centerline exemption (the crown ridge is the profile's domain; so is the
# whole longitudinal profile).  Stations cluster the ring's OWN vertices along the
# ref axis at ``RUNWAY_STATION_CLUSTER_M`` — the SAME 5.0 m convention
# ``verification._runway_single_poly_cross_stations`` uses to reconstruct the
# profile, so the LATERAL within-shape domain and the LONGITUDINAL profile check
# agree on what a "station" is.
#
# LEGACY INVARIANCE BY CONSTRUCTION: a segmented 4-corner runway piece projects
# to exactly TWO extreme axis stations, so every pair is same- or adjacent-
# station and the scoping is a NO-OP (gate-off within unchanged).  A crowned
# sub-rect's inserted centerline vertex sits at the same station as its cross-edge
# corners, so it stays two stations too.
RUNWAY_STATION_CLUSTER_M = 5.0


def runway_axis_station_indices(ring):
    """Assign each vertex of a runway RING a longitudinal STATION index along the
    runway's ref axis.

    The ref axis is the ring's longest vertex pair (the runway diameter, origin
    at one end); every vertex projects to a station distance along it; a vertex
    more than ``RUNWAY_STATION_CLUSTER_M`` beyond the previous vertex in ascending
    station order opens a new cluster.  Returns a list ``station[i]`` parallel to
    ``ring`` (0 = the end at the axis origin, increasing along the axis), or
    ``None`` when the ring is degenerate (<2 vertices or a zero-length axis — no
    stations to scope by, so the caller keeps every pair).

    Same 5.0 m chained clustering as
    ``verification._runway_single_poly_cross_stations``: the lateral within-shape
    domain and the longitudinal profile check must agree on what a station is.  A
    legacy 4-corner runway piece has its corners at two extreme stations only, so
    it yields station indices in {0, 1} — the adjacency predicate below then
    passes every pair (no-op)."""
    n = len(ring)
    if n < 2:
        return None
    # Longest vertex pair = the runway ref axis (origin at A, unit direction A→B).
    best = -1.0
    ax = ay = bx = by = 0.0
    for i in range(n):
        xi, yi = ring[i]
        for j in range(i + 1, n):
            xj, yj = ring[j]
            d2 = (xj - xi) ** 2 + (yj - yi) ** 2
            if d2 > best:
                best, ax, ay, bx, by = d2, xi, yi, xj, yj
    if best <= 0.0:
        return None
    length = best ** 0.5
    ux, uy = (bx - ax) / length, (by - ay) / length
    stations = [((ring[i][0] - ax) * ux + (ring[i][1] - ay) * uy)
                for i in range(n)]
    order = sorted(range(n), key=lambda i: stations[i])
    station_of = [0] * n
    cluster = 0
    previous = stations[order[0]]
    for k in range(1, n):
        i = order[k]
        if stations[i] - previous > RUNWAY_STATION_CLUSTER_M:
            cluster += 1
        station_of[i] = cluster
        previous = stations[i]
    return station_of


def runway_within_pair_in_domain(station_a: int, station_b: int) -> bool:
    """A runway RING vertex pair is in the WITHIN-SHAPE (lateral) grade domain
    iff its endpoints are the SAME station or ADJACENT stations
    (``|Δ station index| <= 1``).  A pair spanning 2+ station intervals is a
    LONGITUDINAL grade the FAA profile law owns (``check_runway_profile`` + the
    spine-profile check), NOT the lateral within-shape check — counting it here
    double-books an at-cap longitudinal chord against a check with no
    jurisdiction over it (user ruling 2026-07-08; extends the part-30i
    crown-centerline exemption).  On a legacy 4-corner runway piece (two
    stations) every pair is same/adjacent → this is a no-op."""
    return abs(station_a - station_b) <= 1


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
    ``mesh_member_fn``   returns whether the pair is a triangle-mesh edge of the
                         shape's ring (junction mesh rule); the reader sets it to
                         None unless the rule can apply (gate on, junction role,
                         non-spine, non-ring-adjacent).  None ⇒ no mesh
                         restriction — a reader that cannot triangulate stays
                         STRICTER (checks every body chord), never looser.
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
    mesh_member_fn: Optional[Callable[[], bool]] = None
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
    # — an ALONG-SEAM pair (both endpoints DEM-pinned) is terrain-controlled.
    #   A pair with ONE seam endpoint stays IN the law (2026-07-03, user
    #   SPLP report): the blanket skip left the APPROACH to the seam pin
    #   ungraded on both readers — the solver never spread the drop and the
    #   validator never flagged it, so a taxiway crossing a tile line dove
    #   into a V-notch at the pin (SPLP: mirrored 1.2-1.3 m dips both tile
    #   sides, law-true 0).  With the pair kept, the seam node is a hard
    #   anchor the surface must RAMP to at the shape's own cap.
    #   RUNWAY-family pairs keep the full exemption for now: the FAA
    #   profile is solved separately and a mid-runway seam pin can
    #   contradict it locally (SPLP: 4.2 m notch) — the profile-side fix
    #   (seam anchor as a regrade target) is queued.
    if p.a_seam and p.b_seam:
        return SKIP
    if ((p.a_seam or p.b_seam)
            and p.role in ("runway", "runway_crossing")):
        return SKIP
    # — both ends on building pads ⇒ inter-pad frontage = an allowed building
    #   ↔building step, not an apron grade path.
    if p.a_building and p.b_building:
        return SKIP
    # — sub-noise separation is not a grade constraint.
    if p.dist < MIN_PAIR_DIST_M:
        return SKIP
    # — JUNCTION MESH RULE (O4_JUNCTION_MESH_CONSTRAINTS, user 2026-06-30): a
    #   junction's only real grade paths are its SPINE and the triangle-mesh
    #   edges of its ring (what X-Plane's mesh renders); every other body
    #   chord is phantom — an aircraft follows the spine, not the diagonal —
    #   and mesh compliance already implies straight-chord compliance.  So a
    #   junction-role pair that is not ring-adjacent, shares no spine
    #   centerline, and is not a mesh edge is not a regulated grade path.
    #   APRONS are NOT mesh-restricted (their geodesic flatness model catches
    #   aggregate slope a mesh edge misses) — the reader never supplies the
    #   thunk for them.  Sits BEFORE the visibility skip so a phantom chord
    #   never pays for the polygon-containment test.
    if (JUNCTION_MESH_CONSTRAINTS and p.role in JUNCTION_ROLES
            and not p.ring_adjacent and not p.spine_caps
            and p.mesh_member_fn is not None and not p.mesh_member_fn()):
        return SKIP
    # — a non-adjacent chord that leaves the pavement is not a surface path.
    if not p.ring_adjacent and p.visible_fn is not None and not p.visible_fn():
        return SKIP
    # — the climb between the two sides is carried by the SPINE at the taxi cap;
    #   the straight diagonal across it is not an independent grade path.
    #   NEVER for a RING-ADJACENT pair (user 2026-07-04): a ring edge is a
    #   physical stretch of pavement surface, not a chord — skipping it
    #   leaves adjacent emitted vertices with NO law edge, so the final
    #   projection's anchor-reach envelope clamps them independently and
    #   imprints its per-node reach noise on the surface (SPLP seam
    #   approach: ±1 m wiggles at 10-14 % between ring neighbours whose
    #   pin-derived ceilings differed by more than any legal edge).
    if (not p.ring_adjacent
            and p.crosses_spine_fn is not None and p.crosses_spine_fn()):
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

    # SEAM PINS ARE GRADED-TO HARD ANCHORS (user 2026-07-04, "treat the
    # seam like a runway edge or building"): a pair with a seam-pinned
    # endpoint never earns spine/blend credit — those credits describe
    # travel ALONG a route, but the approach to an immovable terrain pin
    # is the shape's own grading problem at its own body cap (SPLP: spine
    # credit legalised a 2.5-2.8 % V-notch approach to a band-edge pin
    # the projection had left 0.7-1.1 m below its neighbours).  The road
    # carve below still relaxes (a service road descends to ITS seam pin
    # at the road grade).
    if (p.a_seam or p.b_seam) and cap > p.body_cap:
        cap = p.body_cap

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
