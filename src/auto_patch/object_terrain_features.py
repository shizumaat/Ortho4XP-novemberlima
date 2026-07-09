"""Recognize tunnel and taxiway-bridge structures from placed OBJ8 geometry.

Workstream W-R3 of ``docs/object_terrain_features_spec.md`` (section 3.1).
This is the shared, PURE classifier that feeds the two gated terrain
features: feature A (tunnel cutouts, W-T) and feature B (bridge
adaptation, W-B).  Geometry and placements in, records out — no file
discovery, no ``DSFTool`` invocation, no mesh sampling.  Callers hand it
already-read placements (``obj8_reader.read_dsf_object_placements``) and
already-loaded geometry (``obj8_reader.load_object_file``).

The physics the recognizers rest on
-----------------------------------
X-Plane places each object rigidly at ``terrain(anchor) + offset``, where
``offset`` is 0 for a plain ``OBJECT``, the signed above-ground metres for
``OBJECT_AGL`` and (absolute, handled separately) for ``OBJECT_MSL``.  A
structure's parts share (very nearly) one anchor terrain, so the classifier
reasons about height in an anchor-independent **effective height**::

    effective_y = placement.above_ground_level_metres + authored_local_y

which drops the unknown, shared ``terrain(anchor)`` constant.  The plane
``effective_y = 0`` is grade.  This is what lets a below-grade tunnel
authored 0..+7 and placed at ``OBJECT_AGL -7`` (EGLL tunnels 6/7/10) be
read on the same footing as a plain tunnel authored 0..-5 and placed at 0:
both have their roof slab at ``effective_y ≈ 0`` and their deck well below
it (spec section 2.1).

The world frame
---------------
All polygons and lines in the emitted records live in ONE per-structure
metre frame, following ``object_anchor``'s convention: an unrotated
east-north-up frame whose origin is the mean of the structure's placement
longitudes/latitudes (a synthetic heading-0 placement at that origin).
Shapely coordinates are ``(x, z)`` = ``(metres east, metres south)`` — so
polygon ``.area`` is already in square metres — and the vertical axis is
``effective_y``.  Convert a frame polygon back to longitude/latitude with
:func:`frame_polygon_to_longitude_latitude` (or a point with
``obj8_reader.local_offset_to_lonlat(origin_latitude, origin_longitude,
0.0, x, z)``).  Every record carries ``frame_origin_longitude_latitude``
so downstream code can make that conversion; this field is additive to the
spec's record sketch, which lists the geometry without saying how to place
it in the world — it cannot be omitted without making the polygons
unusable.

Up-facing, and why winding is not trusted
-----------------------------------------
The EGLL investigation found triangle-winding normals unreliable — flipped
in the deck objects — so the probe code classified faces with the STORED
vertex normals.  ``ObjectGeometry`` (frozen contract) does not carry stored
normals, and the supervisor-granted loader extension was for hardness only,
not normals.  The classifier therefore uses the one face signal that is
INDEPENDENT of winding sign: near-horizontality, ``|n_y| / |n| ≥
NEAR_HORIZONTAL_NORMAL_Y_MIN``.  A winding flip negates every normal
component, leaving that ratio unchanged.  Roof, deck and ceiling are then
separated by their effective height, which needs no normal sign — a roof
slab and its ceiling underside share a footprint, so the covered roof
footprint (roof ∩ deck) and the open mouths (deck − roof) come out right
regardless of which way any single face is wound.

Grouping
--------
A tunnel's shell and deck arrive as separate resources sharing one anchor
area (EGLL ``N.obj`` / ``Na.obj``); a KBNA bridge is six parts on one
shared anchor.  Grouping reuses ``object_anchor.discover_object_pools`` —
the existing world-footprint overlap primitive — so no new gap heuristic is
invented (spec pre-reading, ``docs/obj8_structure_partition.md``).  Parts of
one bridge or tunnel share a footprint and overlap by many metres; distinct
structures are hundreds of metres apart, so the small expansion epsilon is
not sensitive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median
from typing import Iterable, Sequence

import numpy
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from . import obj8_reader
from .object_anchor import discover_object_pools
from .obj8_reader import ObjectGeometry, ObjectPlacement

# Exceptions the shapely combinators here may legitimately raise on
# degenerate input (shapely-domain only — never built-ins, which would
# mask a real bug; the project-standard guard, see object_footprints).
try:  # shapely 2
    from shapely.errors import GEOSException as _GEOS_EXCEPTION
except ImportError:  # pragma: no cover - shapely 1 fallback
    from shapely.errors import TopologicalError as _GEOS_EXCEPTION


# ---------------------------------------------------------------------------
# Detection thresholds (spec section 3.1) — named constants, no magic
# numbers at the call sites (pattern: LEVEL_DRAPED_MAX_ABS in
# dsf_road_network.py).
# ---------------------------------------------------------------------------

# A tunnel body's deck sits at least this far below grade (metres).
TUNNEL_MIN_BODY_DEPTH_M = 2.0

# A tunnel is a structure with at least this much near-horizontal DRIVABLE
# (hard) deck area below grade — the discriminator against both bridges
# with deep piers (below-grade geometry vertical, not horizontal) and
# buried building BASEMENTS.  The EGLL below-grade-versus-author-mesh
# correlation (2026-07-09) is decisive: 100% of below-grade building
# pieces at EGLL are buried by the author's own mesh (deepest: T2_T3_3 at
# −9.22 m) and NONE of the 30 below-grade buildings carries ANY below-grade
# hard area, while 17/20 tunnel objects carry hard_deck with large
# below-grade deck areas (tunnel 2a: 17,046 m²).  Depth alone is exactly
# INVERTED as a signal — the deepest objects are basements; exposed tunnels
# run only −4…−5 m.  The at-grade hard-deck road object (T2_3/ROADT23) is
# excluded by the below-grade requirement, not by hardness.
TUNNEL_MIN_BELOW_GRADE_DECK_AREA_M2 = 200.0

# A negative OBJECT_AGL placement offset of at least this magnitude flags a
# below-grade structure on its own — the three EGLL AGL tunnel shells
# (6/7/10) carry no hard triangles at all, and tunnel 10 sits at exactly
# −1.0 m, so the threshold is 1.0, not the body-depth 2.0.
TUNNEL_MIN_BELOW_GRADE_AGL_OFFSET_M = 1.0

# The AGL limb applies only to SINGLE-placement resources (spec section
# 2.1 lists "single placement" among the tunnel signatures) that carry at
# least this much near-horizontal solid area below effective grade.
# Round-5 measurement over every AGL ≤ −1 resource at EGLL: the three
# true AGL tunnels (6/7/10) are single-placement with 50/90/55 m² of
# below-grade deck; the false positives are `Docking_fit_wall_5m5` (36
# placements, offsets mixed −1.0 to +2.0 — the SAME resource above and
# below grade) and three `fit_wall_24x` walls (single placement, 0 m²
# below-grade horizontal area — pure vertical geometry).  Without the
# guard the docking wall seeded a component spanning every gate line and
# cascaded 280+ jetways/marks/terminal pieces onto the R4 exclusion list.
TUNNEL_AGL_MIN_BELOW_GRADE_DECK_AREA_M2 = 25.0

# A roof/deck face counts as "at grade" when its effective height is within
# this tolerance of the grade plane; the deck is everything below it.
TUNNEL_ROOF_TOP_TOLERANCE_M = 0.5

# The near-horizontal HARD plane set must cover at least this area to be a
# bridge deck (rejects railings and clutter).  Amendment A4: "hard" means
# ATTR_hard_deck OR plain ATTR_hard — the KMCO humped bridges (36/112 plain
# ATTR_hard triangles, zero hard_deck) and the EDDF A3 ramp carry no
# hard_deck at all; keying on hard_deck alone misses every humped bridge
# measured.
BRIDGE_MIN_DECK_AREA_M2 = 200.0

# Deck-top profile bin length along the bridge axis (spec section 3.1 as
# amended by A2: a single deck plane cannot represent the KMCO/KDFW
# rising-bridge class — ruling R9, the vertical split is derived).
BRIDGE_PROFILE_BIN_LENGTH_M = 10.0

# A deck profile is non-flat (PROFILE_CARRIED candidate, amendment A4) when
# the crest stands at least this far above the LOWER of the two profile
# ends (supervisor ruling, 2026-07-09 round 3: crest − minimum end, not
# crest − maximum end).  This captures both the crowned KMCO humps (crest
# +5.15 over ends ≈ 0) and the MONOTONE EDDF A3 ramp (crest +6 AT one end,
# the other at grade): pavement drapes over the whole slope (35.5% coverage
# measured), so the terrain must follow it — reading a monotone ramp as
# flat/deck-carried would leave its causeway unbuilt.  The flat EDDF/KBNA
# decks rise 0 under either formula.
BRIDGE_PROFILE_NON_FLAT_MIN_M = 1.0

# Per-end abutment test (amendment A4): solid geometry of ANY hardness must
# reach effective grade within this horizontal radius of each deck-profile
# end, or the structure is a piered viaduct and is REFUSED (a deck-end pin
# on a viaduct would build a false causeway).  Measured end-to-nearest-
# grounded-vertex distances on every true bridge in the four packs
# (2026-07-09): EDDF Bridge_4 0.0/0.1 m, KMCO puente 2.2/11.5 m, puente2
# 2.6/9.8 m, KBNA taxiway-L 5.9/6.6 m (the embankment CLADDING grounds —
# the deck itself ends at +6; the test is "solid geometry reaches grade
# NEAR the end", never "the deck reaches grade"), KBNA Crossing 9.0/15.3 m,
# EDDF Bridge_2 15.5/16.0 m, EDDF Tunnel_1 pair 9.3/18.7 m, EDDF Bridge_3
# 26.6/29.4 m (worst case).  35 m covers all measured cases with margin;
# the KMCO via_tren rail viaduct has NO grounded vertex anywhere (global
# minimum y +3.45) and fails at any radius.
ABUTMENT_GRADE_SEARCH_RADIUS_M = 35.0

# The clearance-limiting underside plane must carry at least this area to
# count (filters stray clutter faces hanging below the girder line).
CLEARANCE_PLANE_MINIMUM_AREA_M2 = 10.0

# An underside plane below this height cannot be the ceiling of an opening
# traffic passes through — it is ground furniture (embankment cladding
# footings measured at +0.55/+0.64 under the KBNA decks), not a girder
# line.  Same physical floor as the deck-carried height threshold: an
# opening lower than a deck-carried deck's minimum height is not a
# corridor.
CLEARANCE_MINIMUM_OPENING_HEIGHT_M = 2.0

# A deck standing at least this far above grade is deck-carried on its own
# structure; a deck flush at grade (≈0) is terrain-carried.
BRIDGE_DECK_CARRIED_MIN_HEIGHT_M = 2.0

# Contract by pavement coverage of the mid-deck box (spec section 2.3): at
# or below the first fraction is deck-carried (pavement cut at the
# abutments); at or above the second is terrain-carried (pavement drapes
# across the span); the band between is refused as AMBIGUOUS (ruling R5).
BRIDGE_CONTRACT_PAVEMENT_COVERAGE_DECK_CARRIED_MAX = 0.05
BRIDGE_CONTRACT_PAVEMENT_COVERAGE_TERRAIN_CARRIED_MIN = 0.30

# A face is near-horizontal when the magnitude of its unit normal's
# vertical component reaches this — winding-sign independent (module
# docstring).  0.7 admits the gentle tunnel-mouth ramps as deck faces.
NEAR_HORIZONTAL_NORMAL_Y_MIN = 0.7

# Solid geometry within this height of grade is "ground-touching" — the
# abutment/ground-contact test (mirrors DSF_OBJECT_ELEVATED_BASE_M).
GROUND_CONTACT_TOLERANCE_M = 0.5

# Placements whose expanded world footprints overlap by this margin pool
# into one structure (module docstring, "Grouping").
STRUCTURE_GROUPING_EPSILON_M = 2.0

# ---------------------------------------------------------------------------
# Round-5 mega-pool refinement (A9/A10 worklist).  discover_object_pools
# merges everything whose bounding boxes chain-overlap — at EGLL the 20
# tunnel objects pooled with terminals and clutter into 5 mega-pools,
# diluting every tunnel metric (pool body-depth medians 0.94-1.93 m versus
# the true 4-7 m decks) and ballooning the R4 exclusion list to 812
# objects.  Features are therefore classified per CONTRIBUTING COMPONENT
# inside each pool, and records/exclusions carry only contributing
# resources.
# ---------------------------------------------------------------------------

# Below-grade drivable seeds whose footprints come within this distance
# join one tunnel/cutout component (a shell and its deck overlap; distinct
# tunnels are hundreds of metres apart).
TUNNEL_COMPONENT_JOIN_BUFFER_M = 2.0

# Hard-face seeds within this distance join one bridge component (the six
# KBNA taxiway-L part objects abut within metres; distinct bridges are
# hundreds of metres apart).
BRIDGE_COMPONENT_JOIN_BUFFER_M = 10.0

# A non-seed resource joins a tunnel/cutout component when at least this
# fraction of its own footprint lies over the component's below-grade deck
# (buffered by the join buffer) — the roof SHELL over its deck.  A
# terminal standing over a small tunnel overlaps only fractionally and
# stays out.
TUNNEL_COVER_CONTAINMENT_MIN_FRACTION = 0.5

# Effective heights are clustered into bins of this size to find a
# dominant plane (deck top, girder ceiling).
PLANE_HEIGHT_BIN_M = 0.5

# Morphological close applied to a triangle-footprint union, closing the
# hairline seams the triangle soup leaves (probe value).
FOOTPRINT_CLOSE_M = 0.05

# The roof footprint is dilated by this before subtraction so that a mouth
# is the deck genuinely clear of the slab, not a seam-width sliver.
ROOF_DIFFERENCE_BUFFER_M = 0.2

# A bridge deck is often several part objects (KBNA taxiway-L is six); this
# larger close welds the part seams into one deck footprint.
BRIDGE_DECK_CLOSE_M = 1.0

# Mouth / footprint fragments smaller than this are seam noise, discarded.
MINIMUM_FEATURE_AREA_M2 = 5.0

# A ceiling plane must sit at least this far below the deck top to be the
# clearance underside rather than the deck slab's own bottom face.
CEILING_MINIMUM_GAP_BELOW_DECK_M = 0.5

# Case-insensitive substring that marks a cosmetic (hard-less) bridge
# resource (Murfreesboro class).  Applies ONLY to structures with NO hard
# triangles at all; any structure with hard geometry goes through the
# geometric deck path, name-independent (the KMCO "puente" objects are
# named in Spanish — a name hint must never gate a hard deck).
COSMETIC_BRIDGE_NAME_HINT = "bridge"

# Contract labels (spec sections 2.3 / 3.1 / 3.2, amended by A4).
DECK_CARRIED = "DECK_CARRIED"
TERRAIN_CARRIED = "TERRAIN_CARRIED"
PROFILE_CARRIED = "PROFILE_CARRIED"
AMBIGUOUS = "AMBIGUOUS"

# Which evidence path produced the contract (BridgeStructure.contract_evidence,
# A10 worklist: tools must be able to print it).
CONTRACT_EVIDENCE_PAVEMENT_COVERAGE = "pavement_coverage"
CONTRACT_EVIDENCE_DECK_PROFILE = "deck_profile_fallback"

# The contract coverage band spans the middle third of the deck ALONG the
# axis and — A10 round-5 calibration — the central HALF of the deck ACROSS
# the axis.  Measured at KBNA taxiway-L (the deck-carried flagship): every
# draped-pavement overlap with the 131 × 55 m deck footprint hugs a lateral
# edge (across-axis positions [−15, 0] and [−55, −43] on a [−55, 0] deck) —
# adjacent AT-GRADE taxiways lapping the deck's side, not span-crossing
# pavement — and the full-width band read them as 14.5% coverage, landing
# the flagship in the refusal dead band.  The carried surface runs the deck
# CENTER; the central-half band measures 0% there while keeping the
# genuinely continuous EDDF/KMCO drapes (centered along the deck) intact.
BRIDGE_COVERAGE_BAND_WIDTH_FRACTION = 0.5

# Deck hardness kinds (BridgeStructure.deck_hardness): which OBJ8 collision
# attribute carries the drivable surface.  Ruling R8's flush-seating cut
# keys on genuine ATTR_hard_deck.
DECK_HARDNESS_HARD_DECK = "hard_deck"
DECK_HARDNESS_HARD = "hard"
DECK_HARDNESS_COSMETIC = "cosmetic"

# ---------------------------------------------------------------------------
# Feature C — structure ground interfaces (spec section 3.4, amendments
# A5-A8, rulings R5-as-refined and R10).
# ---------------------------------------------------------------------------

# The footprint boundary is divided into this many radial sectors around
# the structure centroid for the perimeter base profile.  36 sectors of
# 10 degrees make one sector 2.8% of the perimeter, so the 5% share floor
# below is a real filter (with 16 sectors any single occupied sector would
# already pass it).
PERIMETER_SECTOR_COUNT = 36

# A vertex column is a WALL column when its vertical extent reaches this
# (amendment A5, normative): roof-overhang and draped-decal edges otherwise
# dominate the base profile as false levels.
WALL_COLUMN_MIN_VERTICAL_EXTENT_M = 2.5

# Solid vertices are grouped into columns on this horizontal grid.
WALL_COLUMN_GRID_M = 1.0

# The per-sector facade-base low envelope is this percentile of wall-column
# bases (amendment A5: "minimum (≈ 5th percentile)"; never the dominant or
# highest band — the dominant band is exactly the ELLX/LFPG decoy).
FACADE_BASE_LOW_ENVELOPE_PERCENTILE = 5.0

# Interface levels are clustered at this granularity (amendment A5; keeps
# the LFLL −2 m mezzanine and −10 m rail floor distinct).
INTERFACE_LEVEL_CLUSTER_M = 0.5

# A clustered level below this perimeter share is a basement/service
# parasite and is dropped (amendment A5) — UNLESS the level is below grade
# and carried by the structure's dominant-area object (amendment A7, the
# LFPG T1 lesson: the share filter must never kill the main floor).
INTERFACE_LEVEL_MIN_PERIMETER_SHARE = 0.05

# Ground contact: solid faces whose height is within this band of
# effective grade count as ground-contact geometry.
GROUND_CONTACT_BAND_HALF_WIDTH_M = 1.0

# A structure is a BOWL candidate when its overall ground-contact fraction
# falls below this (amendment A7: "essentially no ground-contact
# geometry"; LFPG T1 measured 0-23% per OBJECT — the structure-level cut
# sits at 10%, the least-measured constant of this family, flagged in the
# workstream report)...
BOWL_MAX_GROUND_CONTACT_FRACTION = 0.10

# ...and its at-grade wall-column base share falls below this: a bowl
# structure has essentially NO facade based at grade (measured: LFPG T1
# pool 0.05 versus the LFPG T2A spine pool 0.32 and EGLL buried terminals
# ≈ 0.5 — the trench/buried structures keep their at-grade halls, the
# bowl floats entirely above its sunken floor).  This pair of gates
# implements amendment A7's "dominant-area object bases below grade" in
# the form that actually measures: the LITERAL dominant-area object in
# these packs is a mega-bake whose MEDIAN column base is elevated
# (+32 m at T1, +14 m at T2A — rooftop columns), so the dominant-object
# base is mechanically useless as the bowl key; the at-grade base share
# is the signal A7's evidence (0-23% ground contact, shell base −3.43)
# was actually pointing at.
BOWL_MAX_AT_GRADE_BASE_SHARE = 0.10

# ...and the structure carries a below-grade interface level at least
# this deep.  Round-5 calibration against the EGLL full-pack run: every
# TRUE bowl measures −3.41 m or deeper (LFPG T1 shell −3.42, satellite
# ring pools −3.4/−3.9/−4.5), while every false positive is buried
# library-clutter slack between −1.03 and −2.47 (fuel tank −1.50, sheds
# −2.08, truck/factory pair −2.47) — sunk-object slack the A6 oracle says
# to bury under flat terrain.  3.0 sits between the measured sets.
BOWL_MIN_BELOW_GRADE_LEVEL_DEPTH_M = 3.0

# Bowl and trench records are only emitted for structures at least this
# large, guarding against small AGL-placed clutter (jetway slack, sunk
# signage) reading as terrain features.  NOT measurement-derived — a
# conservative invented floor, flagged in the workstream report.
STRUCTURE_INTERFACE_MIN_FOOTPRINT_AREA_M2 = 500.0

# A TRENCH_SPINE interface level sits at least this far below grade (the
# LFLL −2 m mezzanine is NOT a trench floor; the −7.5 m LFPG T2 and −10 m
# LFLL levels are)...
TRENCH_SPINE_MIN_DEPTH_M = 2.5

# ...and must be carried by at least this many objects (LFPG T2: 23
# objects share one −7.5 m level over 3 km; one object's private basement
# is not a spine)...
TRENCH_SPINE_MIN_CONTRIBUTING_OBJECTS = 2

# ...and the trench LEVEL itself must hold at least this perimeter share:
# a spine is the structure's defining below-grade interface, not a
# minority stagger.  Measured: true trench levels hold 0.50 (LFPG K5
# pool) and 0.639 (T2A spine) of their occupied sectors; the false
# positives hold 0.033-0.067 (EGLL basement parasites) and 0.057 (the
# KBNA Metropolitan downtown-skyline bake, whose building bases stagger
# 0 to -2.56 down a real city slope).  0.25 sits a factor of two from
# both measured sets.
TRENCH_SPINE_MIN_LEVEL_PERIMETER_SHARE = 0.25

# ...and the below-grade content footprint's LARGEST CONNECTED PART must
# reach this area.  A trench the terrain must open is one COHERENT
# corridor (the LFPG T2A spine is a single contiguous ribbon over 3 km;
# the smallest true LFPG trench pool measures 8,885 m²), never scattered
# pockets: summing disjoint pieces let 276 EGLL jetway-leg slack specks
# plus hotel basements masquerade as a "multi-object trench" in the
# round-5 full-pack run (the EGLL T2_3 buried-basement group alone is
# 48 m² — A6 oracle: buried, flat).  Largest-part gating, not sum
# gating.
TRENCH_SPINE_MIN_FOOTPRINT_AREA_M2 = 1000.0

# Interior cutout (ruling R10, guards calibrated by amendment A8): the
# below-grade DRIVABLE content must be at least this fraction enclosed
# within the structure's own at-grade plan footprint (KDEN platforms are
# 100% enclosed; EGLL tunnel decks are open at the mouths).
INTERIOR_CUTOUT_ENCLOSURE_MIN_FRACTION = 0.95

# A pool with at least this many wall columns is a BUILDING and never
# enters the bridge path — a terminal with a drivable elevated roadway
# baked in (the ELLX departures ramp is plain ATTR_hard) would otherwise
# be swallowed as a bridge candidate and refused by the viaduct guard,
# leaving no feature-C record.  Measured wall-column counts: every true
# bridge pool across KBNA/KMCO/EDDF has 0-169 (via_tren 0, KBNA
# taxiway-L 132, EDDF Tunnel_1 pair 169); the ELLX terminal pool has
# 6,752 and the LFPG terminal pools 1,968-13,511 — a 40x gap around this
# floor.  The tunnel path is deliberately NOT gated on it: cut-and-cover
# shells legitimately carry long wall rows.
BUILDING_MIN_WALL_COLUMN_COUNT = 500

# The at-grade plan footprint is morphologically closed at this radius —
# building bakes carry larger part seams than bridge decks.
AT_GRADE_FOOTPRINT_CLOSE_M = 2.0

# Interface class labels (StructureGroundInterface.interface_class).
INTERFACE_FLAT_CONFIRMED = "FLAT_CONFIRMED"
INTERFACE_BOWL_UNDER_DECK = "BOWL_UNDER_DECK"
INTERFACE_TRENCH_SPINE = "TRENCH_SPINE"
INTERFACE_INTERIOR_CUTOUT = "INTERIOR_CUTOUT"


# ---------------------------------------------------------------------------
# Emitted records (spec section 3.1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MouthDepthStatistics:
    """Ramp-profile depth statistics for one tunnel mouth, in metres BELOW
    grade (positive down), from the deck faces whose centroid falls inside
    the mouth polygon."""

    minimum_depth_m: float
    maximum_depth_m: float
    mean_depth_m: float
    sample_count: int


@dataclass(frozen=True)
class TunnelStructure:
    """A below-grade cut-and-cover structure (EGLL class, feature A).

    ``roof_footprint`` / ``deck_footprint`` / ``mouth_polygons`` are
    shapely polygons in the structure metre frame (module docstring, "The
    world frame"); ``mouth_polygons`` are the open cuts, ``deck −
    roof``.  ``above_ground_offset_m`` is the (signed) placement offset
    carried into every effective height; ``body_depth_m`` is the depth
    (positive) of the roofed body's deck below grade."""

    object_resources: list[str]
    anchor_longitude_latitude: tuple[float, float]
    frame_origin_longitude_latitude: tuple[float, float]
    heading_degrees: float
    placement_kind: str
    above_ground_offset_m: float
    roof_footprint: Polygon | None
    deck_footprint: Polygon | None
    mouth_polygons: list[Polygon]
    mouth_depth_samples: list[MouthDepthStatistics]
    body_depth_m: float


@dataclass(frozen=True)
class BridgeStructure:
    """A taxiway bridge (KBNA / EDDF / KMCO class, feature B).

    ``deck_polygon`` and ``abutment_lines`` are in the structure metre
    frame; ``abutment_lines`` is ordered [start end, far end] along the
    deck axis, matching ``deck_top_profile`` / ``deck_end_elevations_y_m``
    / ``abutment_reaches_grade`` order.

    ``deck_top_profile`` is the deck top's effective height sampled in
    :data:`BRIDGE_PROFILE_BIN_LENGTH_M` bins along the deck's long axis,
    as ``(along_axis_m, y_m)`` pairs from the start end (amendment A2 —
    flat KBNA decks give a constant profile; the KMCO crowned humps and
    sloped ramps give the full shape, ruling R9).

    **Deck extent — the official definition (supervisor ruling, 2026-07-09
    round 3):** the deck is the FULL drivable hard surface, so the profile
    ends where that surface reaches grade, ramps included (KMCO: 392/909 m
    with ends at 0.00, not the 328/820 m between-abutments span).  The pin
    semantics W-B consumes are "profile value at each pavement/terrain
    contact point": a surface whose ramps land at grade pins terrain AT
    grade at the tips, with per-vertex targets along the whole ramp —
    strictly more information than a between-abutments span, and the two
    definitions coincide where a deck has no ramps (KBNA).  Do not
    re-derive a trimmed span downstream.

    ``deck_end_elevations_y_m`` are the first and last profile values (the
    solver pin values at the deck tips); ``deck_top_y_m`` is the profile
    maximum (crest).

    ``ceiling_y_m`` is the LARGEST-area underside plane below the local
    deck top; ``clearance_underside_y_m`` is the LOWEST such plane above
    the opening — the value that limits corridor clearance (KBNA: slab
    underside +4.8 versus girder line +4.2; the corridor emitter needs
    +4.2).  Either is ``None`` when no underside plane was found.

    ``abutment_reaches_grade`` records, per end, whether solid geometry of
    ANY hardness reaches effective grade within
    :data:`ABUTMENT_GRADE_SEARCH_RADIUS_M` of that profile end.  A
    structure failing at either end is a piered viaduct and never becomes
    a ``BridgeStructure`` at all (it lands in
    :attr:`ClassificationResult.refusals`), so on any emitted record both
    entries are ``True``; the field is kept for the audit trail.

    ``deck_hardness`` says which OBJ8 attribute carries the drivable
    surface: :data:`DECK_HARDNESS_HARD_DECK` (genuine ``ATTR_hard_deck``,
    the ruling-R8 flush-seating case), :data:`DECK_HARDNESS_HARD` (plain
    ``ATTR_hard`` — the KMCO/EDDF-ramp class, amendment A4) or
    :data:`DECK_HARDNESS_COSMETIC` (no hard geometry, Murfreesboro class).
    When a deck mixes both hard kinds the dominant kind by face area wins.
    ``hard_deck`` stays the boolean R8 consumers key on: ``True`` ONLY for
    genuine ``ATTR_hard_deck``.

    ``contract`` is one of :data:`DECK_CARRIED`, :data:`TERRAIN_CARRIED`,
    :data:`PROFILE_CARRIED`, :data:`AMBIGUOUS` (spec section 3.2, A4).
    ``absolute_deck_elevation_m`` is the median of the OBJECT_MSL fixtures
    that fall on the deck, else ``None``."""

    object_resources: list[str]
    anchor_longitude_latitude: tuple[float, float]
    frame_origin_longitude_latitude: tuple[float, float]
    heading_degrees: float
    deck_polygon: Polygon | None
    deck_top_profile: list[tuple[float, float]]
    deck_top_y_m: float
    deck_end_elevations_y_m: tuple[float, float]
    deck_length_m: float
    deck_width_m: float
    ceiling_y_m: float | None
    clearance_underside_y_m: float | None
    abutment_lines: list[tuple[tuple[float, float], tuple[float, float]]]
    abutment_reaches_grade: tuple[bool, bool]
    contract: str
    absolute_deck_elevation_m: float | None
    hard_deck: bool
    deck_hardness: str
    # A10 worklist: the measured coverage fraction (None when no pavement
    # evidence was supplied) and which evidence path produced the
    # contract, so audit tools can print both.
    pavement_coverage_fraction: float | None = None
    contract_evidence: str = CONTRACT_EVIDENCE_DECK_PROFILE


@dataclass(frozen=True)
class RefusedStructure:
    """A structure recognized as bridge-like but refused a terrain feature,
    with the reason (amendment A4: the KMCO via_tren piered viaduct must be
    refused — a deck-end pin there would build a false causeway).  Refused
    structures are NOT in the R4 exclusion list: no terrain was adapted to
    them, so the Phase 2 y-bake still applies."""

    object_resources: list[str]
    reason: str


@dataclass(frozen=True)
class StructureGroundInterface:
    """Feature C: what a BUILDING structure's construction says the ground
    must do (spec section 3.4; extraction filters normative per A5, bowl
    rule per A7, interior-cutout rule per R10/A8).

    Emitted for pools that classify as neither tunnel nor bridge and carry
    wall geometry.  ``interface_class`` is one of
    :data:`INTERFACE_FLAT_CONFIRMED` (substantial ground contact — any
    vertical drama above is object-carried, terrain stays flat; the ELLX
    verdict), :data:`INTERFACE_BOWL_UNDER_DECK` (essentially no ground
    contact and the dominant-area object based below grade — LFPG
    Terminal 1), :data:`INTERFACE_TRENCH_SPINE` (a continuous below-grade
    level shared across multiple objects — LFPG Terminal 2 / LFLL rail),
    :data:`INTERFACE_INTERIOR_CUTOUT` (below-grade drivable content
    enclosed within the structure's own at-grade footprint — the KDEN
    train halls, ruling R10).

    ``perimeter_base_profile`` is the facade-base low envelope per
    occupied radial sector, as ``(sector_center_angle_degrees,
    low_envelope_y_m)``; ``interface_levels`` are the surviving clustered
    levels as ``(level_y_m, sector_indices, perimeter_share)`` (the spec
    3.1 sketch).  ``ground_contact_fraction`` is the area share of solid
    faces within :data:`GROUND_CONTACT_BAND_HALF_WIDTH_M` of effective
    grade, overall and per sector (zero-area sectors report 0.0).

    ``at_grade_wall_base_share`` is the fraction of wall columns whose
    base lies within the ground band — the bowl-versus-everything key
    (see :data:`BOWL_MAX_AT_GRADE_BASE_SHARE`).

    ``below_grade_footprint`` (frame metres) is the bowl footprint, the
    trench-spine footprint union, or the enclosed cutout hall, by class
    (``None`` for flat).  ``floor_y_m`` is the matching floor value —
    for a BOWL it is the largest-share below-grade interface level (the
    shell base) and a **bound, not a target**
    (``floor_is_bound_not_target=True``): objects under-specify bowl depth
    (A7: T1 shell base −3.4 m where the reference hand patch cuts −8 m).
    For an INTERIOR_CUTOUT the floor is keyed on the HARD content's
    minimum y — never the deepest solid (KDEN carries non-hard foundation
    piles to −19 m; keying on deepest solid would blow the pocket floor
    9 m past the platform, A8).

    ``elevated_deck_above`` records co-occurring elevated near-horizontal
    geometry over the same footprint — it CONFIRMS a bowl (the T1 helix)
    and is a decoy over a flat structure (the ELLX departures roadway);
    the deciding signal is always the ground-contact fraction (A7).

    KDEN-class ``.agp`` buildings: the classifier is pure and never parses
    ``.agp`` — the CALLER assembles autogen-point part collections
    (``agp_reader`` OBJ_DELTA offsets) into per-part placements sharing
    one anchor, and the ordinary pool grouping absorbs them (A8)."""

    object_resources: list[str]
    anchor_longitude_latitude: tuple[float, float]
    frame_origin_longitude_latitude: tuple[float, float]
    heading_degrees: float
    perimeter_base_profile: list[tuple[float, float]]
    interface_levels: list[tuple[float, tuple[int, ...], float]]
    split_level: bool
    ground_contact_fraction: float
    ground_contact_fraction_by_sector: list[float]
    at_grade_wall_base_share: float
    interface_class: str
    below_grade_footprint: object | None
    floor_y_m: float | None
    floor_is_bound_not_target: bool
    elevated_deck_above: bool


@dataclass(frozen=True)
class ClassificationResult:
    """The classifier's whole output for one pack (spec section 3.1).

    ``exclusions`` is the ruling-R4 feed for the Phase 2 y-bake: every
    consumed tunnel/bridge object, plus every object of a NON-FLAT ground
    interface (split-level structures whose terrain is adapted to them
    join the list exactly like tunnels — spec section 3.4), as
    ``(pack_root, object_resource)``.  FLAT_CONFIRMED interfaces adapt no
    terrain and are not excluded.  ``refusals`` are bridge-like structures
    denied a terrain feature (amendment A4 — piered viaducts); they stay
    OUT of ``exclusions``."""

    tunnels: list[TunnelStructure]
    bridges: list[BridgeStructure]
    exclusions: list[tuple[str, str]] = field(default_factory=list)
    refusals: list[RefusedStructure] = field(default_factory=list)
    ground_interfaces: list[StructureGroundInterface] = field(
        default_factory=list
    )


# ---------------------------------------------------------------------------
# Frame construction and small geometry helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FrameTriangle:
    """One solid triangle projected into the structure metre frame.

    ``corners`` are three ``(x, effective_y, z)`` points;
    ``horizontal_polygon`` is the ``(x, z)`` projection; ``height_m`` is the
    mean effective height (the stable per-face height used for binning);
    ``hardness`` is the loader's per-triangle collision state (``""`` /
    ``"hard"`` / ``"hard_deck"``)."""

    corners: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
    horizontal_polygon: Polygon
    centroid_xz: tuple[float, float]
    height_m: float
    area_m2: float
    horizontality: float
    hardness: str
    resource_path: str

    @property
    def is_hard(self) -> bool:
        """Drivable-hard in the amendment-A4 sense: ATTR_hard_deck OR plain
        ATTR_hard."""
        return self.hardness in ("hard", "hard_deck")


@dataclass(frozen=True)
class _StructureFrame:
    """One pool's projected geometry: the usable frame triangles, the
    ground-contact evidence, and the frame origin.

    ``grounded_vertices_xz`` holds the frame ``(x, z, resource_path)``
    of every solid vertex whose effective height is at or below
    :data:`GROUND_CONTACT_TOLERANCE_M` — collected from the RAW vertex
    list, not the triangle list, because a perfectly vertical
    pier/abutment face collapses to a zero-area horizontal footprint and
    is dropped from ``triangles``, yet its ground contact is exactly what
    the per-end abutment test must see.

    ``vertex_columns`` groups every solid vertex onto a
    :data:`WALL_COLUMN_GRID_M` horizontal grid: grid key → ``(minimum
    effective y, maximum effective y, contributing resource paths)``.
    Feature C's wall-column extraction reads facade bases from it — again
    from the raw vertex list, because facades ARE the vertical faces the
    triangle list drops."""

    origin_latitude: float
    origin_longitude: float
    triangles: list[_FrameTriangle]
    minimum_effective_height_m: float
    grounded_vertices_xz: list[tuple[float, float, str]]
    vertex_columns: dict[
        tuple[int, int], tuple[float, float, frozenset[str]]
    ]


def _triangle_normal_and_area(
    corner_a: tuple[float, float, float],
    corner_b: tuple[float, float, float],
    corner_c: tuple[float, float, float],
) -> tuple[tuple[float, float, float], float]:
    """Unit normal and area of a triangle in the ``(x, up, z)`` frame."""
    u = (
        corner_b[0] - corner_a[0],
        corner_b[1] - corner_a[1],
        corner_b[2] - corner_a[2],
    )
    v = (
        corner_c[0] - corner_a[0],
        corner_c[1] - corner_a[1],
        corner_c[2] - corner_a[2],
    )
    normal = (
        u[1] * v[2] - u[2] * v[1],
        u[2] * v[0] - u[0] * v[2],
        u[0] * v[1] - u[1] * v[0],
    )
    length = math.sqrt(normal[0] ** 2 + normal[1] ** 2 + normal[2] ** 2)
    if length <= 0.0:
        return (0.0, 0.0, 0.0), 0.0
    return (
        normal[0] / length,
        normal[1] / length,
        normal[2] / length,
    ), 0.5 * length


def _placements_mean_origin(
    placements: Sequence[ObjectPlacement],
) -> tuple[float, float]:
    origin_latitude = sum(
        placement.latitude for placement in placements
    ) / len(placements)
    origin_longitude = sum(
        placement.longitude for placement in placements
    ) / len(placements)
    return origin_latitude, origin_longitude


def _build_structure_frame(
    placements: Sequence[ObjectPlacement],
    geometry_by_resource: dict[str, ObjectGeometry],
) -> _StructureFrame:
    """Project every object's solid triangles into the shared structure
    frame, carrying effective height and per-triangle hardness (module
    docstring).

    Each vertex is placed through ITS OWN object's placement to
    longitude/latitude, then into the pool-mean-origin frame — the exact
    ``object_anchor`` pool-frame construction — with ``effective_y =
    above_ground_level_metres + authored_y``.

    Ground-contact evidence (``minimum_effective_height_m`` and
    ``grounded_vertices_xz``) is collected from the raw solid vertex set,
    not the triangle list: a perfectly VERTICAL pier or abutment face
    collapses to a zero-area horizontal footprint and is dropped from the
    triangle list, yet it is exactly the geometry the abutment tests must
    see (see :class:`_StructureFrame`)."""
    origin_latitude, origin_longitude = _placements_mean_origin(placements)
    triangles: list[_FrameTriangle] = []
    grounded_vertices_xz: list[tuple[float, float, str]] = []
    column_accumulator: dict[tuple[int, int], list] = {}
    minimum_effective_height = math.inf
    for placement in placements:
        geometry = geometry_by_resource.get(placement.resource_path)
        if geometry is None or not geometry.solid_triangles:
            continue
        used_vertex_indices = {
            vertex_index
            for triangle in geometry.solid_triangles
            for vertex_index in triangle
        }
        for vertex_index in used_vertex_indices:
            local_x, local_y, local_z = geometry.vertices[vertex_index]
            effective_y = placement.above_ground_level_metres + local_y
            if effective_y < minimum_effective_height:
                minimum_effective_height = effective_y
            world_latitude, world_longitude = (
                obj8_reader.local_offset_to_lonlat(
                    placement.latitude,
                    placement.longitude,
                    placement.heading_degrees,
                    local_x,
                    local_z,
                )
            )
            frame_x, frame_z = obj8_reader.lonlat_to_local_offset(
                origin_latitude,
                origin_longitude,
                0.0,
                world_latitude,
                world_longitude,
            )
            if effective_y <= GROUND_CONTACT_TOLERANCE_M:
                grounded_vertices_xz.append(
                    (frame_x, frame_z, placement.resource_path)
                )
            column_key = (
                int(round(frame_x / WALL_COLUMN_GRID_M)),
                int(round(frame_z / WALL_COLUMN_GRID_M)),
            )
            column = column_accumulator.get(column_key)
            if column is None:
                column_accumulator[column_key] = [
                    effective_y,
                    effective_y,
                    {placement.resource_path},
                ]
            else:
                if effective_y < column[0]:
                    column[0] = effective_y
                if effective_y > column[1]:
                    column[1] = effective_y
                column[2].add(placement.resource_path)
        hardness_states = geometry.solid_triangle_hardness
        for triangle_index, triangle in enumerate(geometry.solid_triangles):
            corners: list[tuple[float, float, float]] = []
            for vertex_index in triangle:
                local_x, local_y, local_z = geometry.vertices[vertex_index]
                world_latitude, world_longitude = (
                    obj8_reader.local_offset_to_lonlat(
                        placement.latitude,
                        placement.longitude,
                        placement.heading_degrees,
                        local_x,
                        local_z,
                    )
                )
                frame_x, frame_z = obj8_reader.lonlat_to_local_offset(
                    origin_latitude,
                    origin_longitude,
                    0.0,
                    world_latitude,
                    world_longitude,
                )
                effective_y = placement.above_ground_level_metres + local_y
                corners.append((frame_x, effective_y, frame_z))
            unit_normal, area = _triangle_normal_and_area(
                corners[0], corners[1], corners[2]
            )
            if area <= 0.0:
                continue
            horizontal_polygon = Polygon(
                [(corner[0], corner[2]) for corner in corners]
            )
            if not horizontal_polygon.is_valid:
                horizontal_polygon = horizontal_polygon.buffer(0)
            if horizontal_polygon.is_empty:
                continue
            centroid_x = sum(corner[0] for corner in corners) / 3.0
            centroid_z = sum(corner[2] for corner in corners) / 3.0
            height_m = sum(corner[1] for corner in corners) / 3.0
            triangle_hardness = (
                hardness_states[triangle_index]
                if triangle_index < len(hardness_states)
                else ""
            )
            triangles.append(
                _FrameTriangle(
                    corners=(corners[0], corners[1], corners[2]),
                    horizontal_polygon=horizontal_polygon,
                    centroid_xz=(centroid_x, centroid_z),
                    height_m=height_m,
                    area_m2=area,
                    horizontality=abs(unit_normal[1]),
                    hardness=triangle_hardness,
                    resource_path=placement.resource_path,
                )
            )
    if minimum_effective_height is math.inf:
        minimum_effective_height = 0.0
    return _StructureFrame(
        origin_latitude=origin_latitude,
        origin_longitude=origin_longitude,
        triangles=triangles,
        minimum_effective_height_m=minimum_effective_height,
        grounded_vertices_xz=grounded_vertices_xz,
        vertex_columns={
            key: (values[0], values[1], frozenset(values[2]))
            for key, values in column_accumulator.items()
        },
    )


def _union_horizontal(
    triangles: Iterable[_FrameTriangle],
    *,
    close_m: float = FOOTPRINT_CLOSE_M,
    keep_all_parts: bool = False,
) -> Polygon | None:
    """Morphologically closed union of the triangles' ``(x, z)`` footprints,
    or ``None`` when empty.

    ``keep_all_parts=False`` (tunnel roof/deck) reduces the result to its
    dominant exterior polygon — one tunnel object welds into one footprint
    and the reduction strips seam noise.  ``keep_all_parts=True`` (a bridge
    deck built from several part objects — KBNA taxiway-L is six) keeps the
    whole union, so a segmented deck is measured across all its parts
    rather than collapsed to the largest single piece."""
    polygons = [
        triangle.horizontal_polygon
        for triangle in triangles
        if not triangle.horizontal_polygon.is_empty
    ]
    if not polygons:
        return None
    try:
        union = unary_union(polygons)
        if close_m > 0.0:
            union = union.buffer(close_m).buffer(-close_m)
        if not union.is_valid:
            union = union.buffer(0)
    except (ValueError, _GEOS_EXCEPTION):
        return None
    if union.is_empty:
        return None
    if keep_all_parts:
        return union if union.geom_type in ("Polygon", "MultiPolygon") else None
    if union.geom_type == "MultiPolygon":
        union = max(union.geoms, key=lambda geometry: geometry.area)
    if union.geom_type != "Polygon":
        return None
    return Polygon(union.exterior)


def _split_polygons(geometry) -> list[Polygon]:
    """Flatten a shapely geometry to its polygon parts above the noise
    area, largest first."""
    if geometry is None or geometry.is_empty:
        return []
    parts = (
        list(geometry.geoms)
        if geometry.geom_type == "MultiPolygon"
        else [geometry]
    )
    kept = [
        part
        for part in parts
        if part.geom_type == "Polygon" and part.area >= MINIMUM_FEATURE_AREA_M2
    ]
    kept.sort(key=lambda part: part.area, reverse=True)
    return kept


def _dominant_height_plane(
    triangles: Sequence[_FrameTriangle],
) -> tuple[float, list[_FrameTriangle]] | None:
    """Cluster faces into :data:`PLANE_HEIGHT_BIN_M` height bins and return
    the area-weighted mean height and members of the largest-area bin."""
    if not triangles:
        return None
    area_by_bin: dict[int, float] = {}
    members_by_bin: dict[int, list[_FrameTriangle]] = {}
    for triangle in triangles:
        bin_key = round(triangle.height_m / PLANE_HEIGHT_BIN_M)
        area_by_bin[bin_key] = area_by_bin.get(bin_key, 0.0) + triangle.area_m2
        members_by_bin.setdefault(bin_key, []).append(triangle)
    dominant_bin = max(area_by_bin, key=lambda key: area_by_bin[key])
    members = members_by_bin[dominant_bin]
    total_area = sum(triangle.area_m2 for triangle in members)
    mean_height = (
        sum(triangle.height_m * triangle.area_m2 for triangle in members)
        / total_area
    )
    return mean_height, members


def frame_polygon_to_longitude_latitude(
    polygon,
    frame_origin_longitude_latitude: tuple[float, float],
):
    """Convert a structure-frame ``(x, z)`` polygon (or MultiPolygon) to
    ``(longitude, latitude)`` for downstream emitters (inverse of the frame
    map)."""
    from shapely.geometry import MultiPolygon

    origin_longitude, origin_latitude = frame_origin_longitude_latitude

    def _convert_ring(coordinates):
        converted = []
        for frame_x, frame_z in coordinates:
            latitude, longitude = obj8_reader.local_offset_to_lonlat(
                origin_latitude, origin_longitude, 0.0, frame_x, frame_z
            )
            converted.append((longitude, latitude))
        return converted

    if polygon.geom_type == "MultiPolygon":
        return MultiPolygon(
            [
                Polygon(_convert_ring(part.exterior.coords))
                for part in polygon.geoms
            ]
        )
    return Polygon(_convert_ring(polygon.exterior.coords))


# ---------------------------------------------------------------------------
# Tunnel recognition (feature A)
# ---------------------------------------------------------------------------

def _agl_tunnel_seed_resources(
    placements: Sequence[ObjectPlacement],
    triangles: Sequence[_FrameTriangle],
) -> set[str]:
    """Resources whose below-grade ``OBJECT_AGL`` placement is a credible
    tunnel signal (the guarded AGL limb — see
    :data:`TUNNEL_AGL_MIN_BELOW_GRADE_DECK_AREA_M2`): single placement,
    offset at or below −:data:`TUNNEL_MIN_BELOW_GRADE_AGL_OFFSET_M`, and
    real below-effective-grade horizontal deck area."""
    placement_count: dict[str, int] = {}
    for placement in placements:
        placement_count[placement.resource_path] = (
            placement_count.get(placement.resource_path, 0) + 1
        )
    below_grade_area: dict[str, float] = {}
    for triangle in triangles:
        if (
            triangle.horizontality >= NEAR_HORIZONTAL_NORMAL_Y_MIN
            and triangle.height_m <= -TUNNEL_ROOF_TOP_TOLERANCE_M
        ):
            below_grade_area[triangle.resource_path] = (
                below_grade_area.get(triangle.resource_path, 0.0)
                + triangle.area_m2
            )
    return {
        placement.resource_path
        for placement in placements
        if placement.above_ground_level_metres
        <= -TUNNEL_MIN_BELOW_GRADE_AGL_OFFSET_M
        and placement_count[placement.resource_path] == 1
        and below_grade_area.get(placement.resource_path, 0.0)
        >= TUNNEL_AGL_MIN_BELOW_GRADE_DECK_AREA_M2
    }


def _is_tunnel_signature(
    placements: Sequence[ObjectPlacement],
    triangles: Sequence[_FrameTriangle],
) -> bool:
    """A structure is a tunnel when it has a substantial near-horizontal
    DRIVABLE deck below grade, or is placed below grade by its OBJECT_AGL
    offset (spec section 3.1; discriminator validated against the EGLL
    author mesh, 0 wrong on 30 below-grade buildings + 20 tunnel objects).

    Two entries, matching the two below-grade signals:

    1. At least :data:`TUNNEL_MIN_BELOW_GRADE_DECK_AREA_M2` of
       near-horizontal HARD (``ATTR_hard`` / ``ATTR_hard_deck``) face area
       at or below :data:`TUNNEL_MIN_BODY_DEPTH_M` beneath grade.  The
       hardness requirement excludes buried building basements (which are
       never drivable — see the area constant's comment); the below-grade
       requirement excludes at-grade hard roads; the near-horizontal
       requirement excludes bridge piers.
    2. A negative OBJECT_AGL placement offset of magnitude
       :data:`TUNNEL_MIN_BELOW_GRADE_AGL_OFFSET_M` or more — the EGLL AGL
       shells (6/7/10) carry no hard triangles, and their offset is the
       unambiguous below-grade signal.
    """
    if not triangles:
        return False
    below_grade_drivable_area = sum(
        triangle.area_m2
        for triangle in triangles
        if triangle.is_hard
        and triangle.horizontality >= NEAR_HORIZONTAL_NORMAL_Y_MIN
        and triangle.height_m <= -TUNNEL_MIN_BODY_DEPTH_M
    )
    if below_grade_drivable_area >= TUNNEL_MIN_BELOW_GRADE_DECK_AREA_M2:
        return True
    return bool(_agl_tunnel_seed_resources(placements, triangles))


def _classify_tunnel(
    placements: Sequence[ObjectPlacement],
    origin_latitude: float,
    origin_longitude: float,
    triangles: Sequence[_FrameTriangle],
) -> TunnelStructure:
    near_horizontal = [
        triangle
        for triangle in triangles
        if triangle.horizontality >= NEAR_HORIZONTAL_NORMAL_Y_MIN
    ]
    roof_faces = [
        triangle
        for triangle in near_horizontal
        if triangle.height_m >= -TUNNEL_ROOF_TOP_TOLERANCE_M
    ]
    deck_faces = [
        triangle
        for triangle in near_horizontal
        if triangle.height_m < -TUNNEL_ROOF_TOP_TOLERANCE_M
    ]
    roof_footprint = _union_horizontal(roof_faces)
    deck_footprint = _union_horizontal(deck_faces)

    mouth_polygons: list[Polygon] = []
    if deck_footprint is not None:
        if roof_footprint is not None:
            try:
                mouth_geometry = deck_footprint.difference(
                    roof_footprint.buffer(ROOF_DIFFERENCE_BUFFER_M)
                )
            except (ValueError, _GEOS_EXCEPTION):
                mouth_geometry = deck_footprint
        else:
            # No roofed section: the whole deck is open cut.
            mouth_geometry = deck_footprint
        mouth_polygons = _split_polygons(mouth_geometry)

    mouth_depth_samples: list[MouthDepthStatistics] = []
    for mouth in mouth_polygons:
        depths = [
            -triangle.height_m
            for triangle in deck_faces
            if mouth.contains(Point(triangle.centroid_xz))
        ]
        if depths:
            mouth_depth_samples.append(
                MouthDepthStatistics(
                    minimum_depth_m=min(depths),
                    maximum_depth_m=max(depths),
                    mean_depth_m=sum(depths) / len(depths),
                    sample_count=len(depths),
                )
            )
        else:
            mouth_depth_samples.append(
                MouthDepthStatistics(0.0, 0.0, 0.0, 0)
            )

    # Body depth: the deck level under the roofed (covered) section.
    if roof_footprint is not None:
        covered_deck_heights = [
            triangle.height_m
            for triangle in deck_faces
            if roof_footprint.contains(Point(triangle.centroid_xz))
        ]
    else:
        covered_deck_heights = []
    if not covered_deck_heights:
        covered_deck_heights = [triangle.height_m for triangle in deck_faces]
    body_depth_m = (
        -median(covered_deck_heights) if covered_deck_heights else 0.0
    )

    reference_placement = placements[0]
    return TunnelStructure(
        object_resources=sorted(
            {placement.resource_path for placement in placements}
        ),
        anchor_longitude_latitude=(
            reference_placement.longitude,
            reference_placement.latitude,
        ),
        frame_origin_longitude_latitude=(origin_longitude, origin_latitude),
        heading_degrees=reference_placement.heading_degrees,
        placement_kind=reference_placement.placement_kind,
        above_ground_offset_m=reference_placement.above_ground_level_metres,
        roof_footprint=roof_footprint,
        deck_footprint=deck_footprint,
        mouth_polygons=mouth_polygons,
        mouth_depth_samples=mouth_depth_samples,
        body_depth_m=body_depth_m,
    )


# ---------------------------------------------------------------------------
# Bridge recognition (feature B)
# ---------------------------------------------------------------------------

def _minimum_rotated_rectangle(polygon):
    """``polygon.minimum_rotated_rectangle`` with GEOS's harmless
    ``oriented_envelope`` divide-by-zero warning (axis-aligned inputs)
    silenced, matching the project's numpy-warning discipline."""
    with numpy.errstate(invalid="ignore", divide="ignore"):
        return polygon.minimum_rotated_rectangle


@dataclass(frozen=True)
class _DeckAxis:
    """The deck's minimum-rotated-rectangle long axis and its two ends.

    ``abutment_lines`` is ordered [start end, far end] along the axis
    (``axis_origin_xz`` + t * ``axis_unit_xz``, t in [0, length]), so
    profile order, end-elevation order and abutment order all agree."""

    length_m: float
    width_m: float
    axis_origin_xz: tuple[float, float]
    axis_unit_xz: tuple[float, float]
    abutment_lines: list[tuple[tuple[float, float], tuple[float, float]]]

    def along_axis(self, x: float, z: float) -> float:
        return (x - self.axis_origin_xz[0]) * self.axis_unit_xz[0] + (
            z - self.axis_origin_xz[1]
        ) * self.axis_unit_xz[1]


def _deck_axis(polygon: Polygon) -> _DeckAxis | None:
    """Long axis + short-edge deck ends of the polygon's minimum rotated
    rectangle.  ``None`` on degenerate geometry."""
    try:
        rectangle = _minimum_rotated_rectangle(polygon)
    except (ValueError, _GEOS_EXCEPTION):
        return None
    if rectangle.geom_type != "Polygon":
        return None
    corners = list(rectangle.exterior.coords)[:4]
    if len(corners) < 4:
        return None
    edges = [
        (corners[index], corners[(index + 1) % 4]) for index in range(4)
    ]
    lengths = [
        math.hypot(end[0] - start[0], end[1] - start[1])
        for start, end in edges
    ]
    long_index = max(range(4), key=lambda index: lengths[index])
    length_m = lengths[long_index]
    width_m = min(lengths)
    if length_m <= 0.0:
        return None
    start, end = edges[long_index]
    axis_unit = (
        (end[0] - start[0]) / length_m,
        (end[1] - start[1]) / length_m,
    )
    # The two short edges are the deck ends; order them along the axis by
    # midpoint projection so [start end, far end] is well defined.
    ordered_by_length = sorted(range(4), key=lambda index: lengths[index])
    short_edges = [edges[ordered_by_length[0]], edges[ordered_by_length[1]]]

    def _midpoint_projection(edge) -> float:
        midpoint_x = (edge[0][0] + edge[1][0]) / 2.0
        midpoint_z = (edge[0][1] + edge[1][1]) / 2.0
        return (midpoint_x - start[0]) * axis_unit[0] + (
            midpoint_z - start[1]
        ) * axis_unit[1]

    short_edges.sort(key=_midpoint_projection)
    abutment_lines = [
        ((edge[0][0], edge[0][1]), (edge[1][0], edge[1][1]))
        for edge in short_edges
    ]
    return _DeckAxis(
        length_m=length_m,
        width_m=width_m,
        axis_origin_xz=(start[0], start[1]),
        axis_unit_xz=axis_unit,
        abutment_lines=abutment_lines,
    )


def _deck_top_profile(
    deck_faces: Sequence[_FrameTriangle],
    axis: _DeckAxis,
) -> list[tuple[float, float]]:
    """Deck TOP effective height in :data:`BRIDGE_PROFILE_BIN_LENGTH_M`
    bins along the deck axis, as ``(along_axis_m, y_m)`` pairs.

    Sampled from deck-face CORNERS (a planar face's height is linear, so
    corners bound it; the end and crest elevations live exactly at
    corners), taking the per-bin MAXIMUM — the top surface — which also
    discards any hard-marked slab UNDERSIDE corners sharing a bin (the
    whole ``ATTR_hard`` range of a slab includes its bottom faces).  Empty
    bins are filled by linear interpolation between their non-empty
    neighbours."""
    corner_samples: list[tuple[float, float]] = []  # (along, y)
    for face in deck_faces:
        for corner_x, corner_y, corner_z in face.corners:
            corner_samples.append(
                (axis.along_axis(corner_x, corner_z), corner_y)
            )
    if not corner_samples:
        return []
    along_positions = [sample[0] for sample in corner_samples]
    along_minimum = min(along_positions)
    along_maximum = max(along_positions)
    span = max(along_maximum - along_minimum, 1e-9)
    bin_count = max(1, int(math.ceil(span / BRIDGE_PROFILE_BIN_LENGTH_M)))
    maximum_by_bin: dict[int, float] = {}
    for along, height in corner_samples:
        bin_index = min(
            bin_count - 1, int((along - along_minimum) / span * bin_count)
        )
        known = maximum_by_bin.get(bin_index)
        if known is None or height > known:
            maximum_by_bin[bin_index] = height
    # Fill empty bins by interpolating between the nearest filled bins.
    filled_indices = sorted(maximum_by_bin)
    profile: list[tuple[float, float]] = []
    bin_width = span / bin_count
    for bin_index in range(bin_count):
        along_center = along_minimum + (bin_index + 0.5) * bin_width
        if bin_index in maximum_by_bin:
            profile.append((along_center, maximum_by_bin[bin_index]))
            continue
        earlier = [index for index in filled_indices if index < bin_index]
        later = [index for index in filled_indices if index > bin_index]
        if earlier and later:
            left, right = earlier[-1], later[0]
            fraction = (bin_index - left) / (right - left)
            interpolated = maximum_by_bin[left] + fraction * (
                maximum_by_bin[right] - maximum_by_bin[left]
            )
        elif earlier:
            interpolated = maximum_by_bin[earlier[-1]]
        else:
            interpolated = maximum_by_bin[later[0]]
        profile.append((along_center, interpolated))
    return profile


def _abutment_reaches_grade_per_end(
    axis: _DeckAxis,
    grounded_vertices_xz: Sequence[tuple[float, float, str]],
) -> tuple[bool, bool]:
    """Amendment A4's viaduct guard, per deck end: does solid geometry of
    ANY hardness reach effective grade within
    :data:`ABUTMENT_GRADE_SEARCH_RADIUS_M` of the end?

    The test point is the abutment line's MIDPOINT (the radius constant
    was calibrated against midpoint distances — see its comment).  The
    DECK need not reach grade: at deck-carried KBNA the deck ends at +6
    while the abutment EMBANKMENT cladding grounds at 0 a few metres away
    — grounded geometry NEAR the end is the signature; a piered viaduct
    (KMCO via_tren, global minimum +3.45) has none anywhere."""
    results = []
    for (start_point, end_point) in axis.abutment_lines:
        midpoint_x = (start_point[0] + end_point[0]) / 2.0
        midpoint_z = (start_point[1] + end_point[1]) / 2.0
        reaches = any(
            math.hypot(grounded_x - midpoint_x, grounded_z - midpoint_z)
            <= ABUTMENT_GRADE_SEARCH_RADIUS_M
            for grounded_x, grounded_z, _resource in grounded_vertices_xz
        )
        results.append(reaches)
    while len(results) < 2:
        results.append(False)
    return results[0], results[1]


def _pavement_coverage_of_mid_deck(
    deck_polygon: Polygon,
    axis: _DeckAxis,
    pavement_frame_union: Polygon | None,
) -> float | None:
    """Fraction of the deck's mid-span band covered by pavement (spec
    section 2.3).  ``None`` when no pavement is supplied.

    The band is the middle THIRD along the deck axis and the central
    :data:`BRIDGE_COVERAGE_BAND_WIDTH_FRACTION` of the deck ACROSS it —
    the across-axis narrowing is the round-5 KBNA calibration (see the
    constant's comment: lateral at-grade taxiways lap the deck's side
    edges and are not span-crossing evidence)."""
    if pavement_frame_union is None:
        return None
    try:
        along_center = axis.length_m / 2.0
        along_half = axis.length_m / 6.0
        unit = axis.axis_unit_xz
        perpendicular = (-unit[1], unit[0])
        origin = axis.axis_origin_xz
        # Centre the band's across coordinate on the deck centroid — the
        # axis origin is a rectangle corner and the deck may extend to
        # either perpendicular side of it.
        centroid = deck_polygon.centroid
        across_center = (centroid.x - origin[0]) * perpendicular[0] + (
            centroid.y - origin[1]
        ) * perpendicular[1]
        across_half = (
            axis.width_m * BRIDGE_COVERAGE_BAND_WIDTH_FRACTION / 2.0
        )
        band_corners = []
        for along in (along_center - along_half, along_center + along_half):
            for across in (
                across_center - across_half,
                across_center + across_half,
            ):
                band_corners.append(
                    (
                        origin[0]
                        + along * unit[0]
                        + across * perpendicular[0],
                        origin[1]
                        + along * unit[1]
                        + across * perpendicular[1],
                    )
                )
        band = Polygon(
            [
                band_corners[0],
                band_corners[1],
                band_corners[3],
                band_corners[2],
            ]
        )
        mid_deck = deck_polygon.intersection(band)
        if mid_deck.is_empty or mid_deck.area <= 0.0:
            return None
        covered = mid_deck.intersection(pavement_frame_union).area
        return covered / mid_deck.area
    except (ValueError, _GEOS_EXCEPTION):
        return None


def _profile_is_non_flat(
    crest_y_m: float,
    deck_end_elevations_y_m: tuple[float, float],
) -> bool:
    """Amendment A4's non-flat test, per the round-3 supervisor ruling: the
    crest stands at least :data:`BRIDGE_PROFILE_NON_FLAT_MIN_M` above the
    LOWER profile end.  Crowned KMCO humps AND monotone ramps (EDDF A3)
    qualify; flat KBNA/EDDF decks do not — see the constant's comment."""
    return (
        crest_y_m - min(deck_end_elevations_y_m)
        >= BRIDGE_PROFILE_NON_FLAT_MIN_M
    )


def _classify_contract(
    crest_y_m: float,
    deck_end_elevations_y_m: tuple[float, float],
    coverage_fraction: float | None,
) -> str:
    """Deck-carried / terrain-carried / profile-carried / ambiguous (spec
    sections 2.3 and 3.2, amended by A4; refusal-not-guessing per R5).

    With pavement coverage: near-zero coverage (pavement cut at the
    abutments) is deck-carried, cross-checked against crest height;
    continuous coverage splits on the measured profile — non-flat ⇒
    PROFILE_CARRIED (the deck profile is the pavement's elevation target;
    flat-across handling would erase the KMCO humps), flat ⇒
    TERRAIN_CARRIED, cross-checked (a flat deck standing high above grade
    with pavement draping across contradicts itself ⇒ AMBIGUOUS).  The
    dead band between the coverage thresholds is AMBIGUOUS.

    Without pavement (coverage ``None``): a non-flat profile with grounded
    abutments (guaranteed upstream — viaducts are refused before this
    point) ⇒ PROFILE_CARRIED; otherwise the crest-height cross-check
    alone."""
    non_flat = _profile_is_non_flat(crest_y_m, deck_end_elevations_y_m)
    height_says_deck_carried = crest_y_m >= BRIDGE_DECK_CARRIED_MIN_HEIGHT_M

    if coverage_fraction is None:
        if non_flat:
            return PROFILE_CARRIED
        return DECK_CARRIED if height_says_deck_carried else TERRAIN_CARRIED

    if coverage_fraction <= BRIDGE_CONTRACT_PAVEMENT_COVERAGE_DECK_CARRIED_MAX:
        return DECK_CARRIED if height_says_deck_carried else AMBIGUOUS
    if (
        coverage_fraction
        >= BRIDGE_CONTRACT_PAVEMENT_COVERAGE_TERRAIN_CARRIED_MIN
    ):
        if non_flat:
            return PROFILE_CARRIED
        return AMBIGUOUS if height_says_deck_carried else TERRAIN_CARRIED
    return AMBIGUOUS


def _classify_bridge(
    placements: Sequence[ObjectPlacement],
    frame: _StructureFrame,
    pavement_frame_union: Polygon | None,
    mean_sea_level_placements: Sequence[ObjectPlacement],
) -> tuple[BridgeStructure | None, str | None]:
    """Classify one pool as a bridge.

    Returns ``(bridge, None)`` on success, ``(None, None)`` when the pool
    is simply not bridge-like, and ``(None, reason)`` when the pool IS
    bridge-like but must be REFUSED a terrain feature (amendment A4's
    piered-viaduct guard)."""
    near_horizontal = [
        triangle
        for triangle in frame.triangles
        if triangle.horizontality >= NEAR_HORIZONTAL_NORMAL_Y_MIN
    ]
    if not near_horizontal:
        return None, None

    # Amendment A4: drivable decks may be plain ATTR_hard (KMCO, the EDDF
    # A3 ramp) — accept both hard kinds as first-class deck faces.
    hard_faces = [
        triangle for triangle in near_horizontal if triangle.is_hard
    ]
    structure_has_hard_geometry = any(
        triangle.is_hard for triangle in frame.triangles
    )
    # ``deck_faces`` is the whole drivable surface (union → deck polygon,
    # corner samples → profile).  A sloped/crowned deck spans many height
    # bins, so the polygon must never be cut to one bin — that fragmented
    # KBNA taxiway-L to 55 m of its 131 m length.
    deck_faces: list[_FrameTriangle]

    if sum(face.area_m2 for face in hard_faces) >= BRIDGE_MIN_DECK_AREA_M2:
        deck_faces = hard_faces
        hard_deck_area = sum(
            face.area_m2
            for face in hard_faces
            if face.hardness == "hard_deck"
        )
        plain_hard_area = sum(
            face.area_m2 for face in hard_faces if face.hardness == "hard"
        )
        # Dominant kind by area; ``hard_deck`` (the R8 flush-seating key)
        # is True only for genuine ATTR_hard_deck decks.
        deck_hardness = (
            DECK_HARDNESS_HARD_DECK
            if hard_deck_area >= plain_hard_area
            else DECK_HARDNESS_HARD
        )
    elif not structure_has_hard_geometry:
        # Cosmetic path (Murfreesboro class), for structures with NO hard
        # triangles anywhere: a broad near-horizontal surface well above
        # grade, ground contact, and a "bridge" name hint.
        name_hint = any(
            COSMETIC_BRIDGE_NAME_HINT in placement.resource_path.lower()
            for placement in placements
        )
        elevated_faces = [
            triangle
            for triangle in near_horizontal
            if triangle.height_m >= BRIDGE_DECK_CARRIED_MIN_HEIGHT_M
        ]
        grounds = (
            frame.minimum_effective_height_m <= GROUND_CONTACT_TOLERANCE_M
        )
        if (
            not name_hint
            or not grounds
            or sum(face.area_m2 for face in elevated_faces)
            < BRIDGE_MIN_DECK_AREA_M2
        ):
            return None, None
        # With no hard attribute to say WHICH elevated surface is drivable,
        # the causeway top is the dominant elevated plane; taking all
        # elevated faces would let truss tops and superstructure rails
        # inflate the profile crest (KBNA Murfreesboro: rails at +11.8 over
        # a +7.4 causeway).
        plane = _dominant_height_plane(elevated_faces)
        if plane is None:
            return None, None
        _plane_height, deck_faces = plane
        deck_hardness = DECK_HARDNESS_COSMETIC
    else:
        # Hard geometry exists but under the deck-area floor: railings and
        # clutter, not a bridge.
        return None, None

    deck_polygon = _union_horizontal(
        deck_faces, close_m=BRIDGE_DECK_CLOSE_M, keep_all_parts=True
    )
    if deck_polygon is None:
        return None, None

    axis = _deck_axis(deck_polygon)
    if axis is None:
        return None, None

    deck_top_profile = _deck_top_profile(deck_faces, axis)
    if not deck_top_profile:
        return None, None
    deck_end_elevations_y_m = (
        deck_top_profile[0][1],
        deck_top_profile[-1][1],
    )
    crest_y_m = max(height for _along, height in deck_top_profile)

    # Amendment A4's viaduct guard: refuse (never emit) a structure whose
    # solid geometry fails to reach grade near EITHER deck end — a deck-end
    # pin there would build a false causeway (KMCO via_tren).
    abutment_reaches_grade = _abutment_reaches_grade_per_end(
        axis, frame.grounded_vertices_xz
    )
    if not all(abutment_reaches_grade):
        failing_ends = [
            "start" if index == 0 else "far"
            for index, reaches in enumerate(abutment_reaches_grade)
            if not reaches
        ]
        reason = (
            "piered viaduct: no solid geometry reaches effective grade "
            f"(≤ {GROUND_CONTACT_TOLERANCE_M} m) within "
            f"{ABUTMENT_GRADE_SEARCH_RADIUS_M:.0f} m of the "
            f"{' and '.join(failing_ends)} deck end(s) — refused, no "
            "terrain feature (amendment A4)"
        )
        return None, reason

    # Underside planes: candidates are near-horizontal faces under the deck
    # footprint that sit a clear gap below the LOCAL deck top (the profile
    # value at their axis position — a crowned deck's own ramp faces are AT
    # the profile and must not read as their own ceiling).
    along_positions = [along for along, _height in deck_top_profile]
    profile_heights = [height for _along, height in deck_top_profile]

    def _local_deck_top(x: float, z: float) -> float:
        along = axis.along_axis(x, z)
        best_index = min(
            range(len(along_positions)),
            key=lambda index: abs(along_positions[index] - along),
        )
        return profile_heights[best_index]

    underside_candidates = [
        triangle
        for triangle in near_horizontal
        if deck_polygon.contains(Point(triangle.centroid_xz))
        and triangle.height_m
        <= _local_deck_top(*triangle.centroid_xz)
        - CEILING_MINIMUM_GAP_BELOW_DECK_M
    ]
    # ceiling_y_m: the LARGEST-area underside plane (the slab underside).
    ceiling_plane = _dominant_height_plane(underside_candidates)
    ceiling_y_m = ceiling_plane[0] if ceiling_plane is not None else None
    # clearance_underside_y_m: the LOWEST underside plane above the opening
    # with real area — the value that limits corridor clearance (KBNA:
    # girder line +4.2 versus slab underside +4.8).
    clearance_underside_y_m = _lowest_underside_plane(underside_candidates)

    coverage_fraction = _pavement_coverage_of_mid_deck(
        deck_polygon, axis, pavement_frame_union
    )
    contract = _classify_contract(
        crest_y_m, deck_end_elevations_y_m, coverage_fraction
    )
    contract_evidence = (
        CONTRACT_EVIDENCE_PAVEMENT_COVERAGE
        if coverage_fraction is not None
        else CONTRACT_EVIDENCE_DECK_PROFILE
    )

    absolute_deck_elevation_m = _median_msl_on_deck(
        deck_polygon,
        frame.origin_latitude,
        frame.origin_longitude,
        mean_sea_level_placements,
    )

    # Round-5 mega-pool refinement: the record carries — and the R4
    # exclusion list receives — ONLY the resources whose geometry actually
    # contributes to the bridge: the deck faces and the underside planes
    # (the latter capture the trench cladding lining the corridor beneath
    # the span — EDDF's Tunnel_N).  Pool co-members (jetways, clutter,
    # passing ground slabs near the abutments) never ride along; grounded
    # geometry stays abutment-test EVIDENCE without becoming a record
    # member, because any grounded clutter within the search radius of an
    # end would otherwise be excluded from the Phase 2 y-bake.
    contributing_resources = {face.resource_path for face in deck_faces}
    contributing_resources.update(
        triangle.resource_path
        for triangle in underside_candidates
        # Structure undersides only: girder/slab planes above the opening
        # (elevated decks) or below-grade trench cladding (flush decks,
        # EDDF Tunnel_N floors at −1).  The band between is at-grade
        # ground furniture passing beneath the span — terrain, not
        # structure, and it must stay y-bakeable.
        if triangle.height_m > CLEARANCE_MINIMUM_OPENING_HEIGHT_M
        or triangle.height_m < -GROUND_CONTACT_TOLERANCE_M
    )

    reference_placement = next(
        (
            placement
            for placement in placements
            if placement.resource_path in contributing_resources
        ),
        placements[0],
    )
    return (
        BridgeStructure(
            object_resources=sorted(contributing_resources),
            anchor_longitude_latitude=(
                reference_placement.longitude,
                reference_placement.latitude,
            ),
            frame_origin_longitude_latitude=(
                frame.origin_longitude,
                frame.origin_latitude,
            ),
            heading_degrees=reference_placement.heading_degrees,
            deck_polygon=deck_polygon,
            deck_top_profile=deck_top_profile,
            deck_top_y_m=crest_y_m,
            deck_end_elevations_y_m=deck_end_elevations_y_m,
            deck_length_m=axis.length_m,
            deck_width_m=axis.width_m,
            ceiling_y_m=ceiling_y_m,
            clearance_underside_y_m=clearance_underside_y_m,
            abutment_lines=list(axis.abutment_lines),
            abutment_reaches_grade=abutment_reaches_grade,
            contract=contract,
            absolute_deck_elevation_m=absolute_deck_elevation_m,
            hard_deck=deck_hardness == DECK_HARDNESS_HARD_DECK,
            deck_hardness=deck_hardness,
            pavement_coverage_fraction=coverage_fraction,
            contract_evidence=contract_evidence,
        ),
        None,
    )


def _lowest_underside_plane(
    underside_candidates: Sequence[_FrameTriangle],
) -> float | None:
    """The LOWEST height bin among underside candidates that (a) carries at
    least :data:`CLEARANCE_PLANE_MINIMUM_AREA_M2` of face area and (b) sits
    high enough to roof an opening
    (above :data:`CLEARANCE_MINIMUM_OPENING_HEIGHT_M` — planes near grade
    are embankment/ground furniture, not a girder line) — the
    clearance-limiting value for the corridor emitter."""
    area_by_bin: dict[int, float] = {}
    members_by_bin: dict[int, list[_FrameTriangle]] = {}
    for triangle in underside_candidates:
        if triangle.height_m <= CLEARANCE_MINIMUM_OPENING_HEIGHT_M:
            continue
        bin_key = round(triangle.height_m / PLANE_HEIGHT_BIN_M)
        area_by_bin[bin_key] = (
            area_by_bin.get(bin_key, 0.0) + triangle.area_m2
        )
        members_by_bin.setdefault(bin_key, []).append(triangle)
    qualifying = [
        bin_key
        for bin_key, area in area_by_bin.items()
        if area >= CLEARANCE_PLANE_MINIMUM_AREA_M2
    ]
    if not qualifying:
        return None
    lowest_bin = min(qualifying)
    members = members_by_bin[lowest_bin]
    total_area = sum(triangle.area_m2 for triangle in members)
    return (
        sum(triangle.height_m * triangle.area_m2 for triangle in members)
        / total_area
    )


def _median_msl_on_deck(
    deck_polygon: Polygon,
    origin_latitude: float,
    origin_longitude: float,
    mean_sea_level_placements: Sequence[ObjectPlacement],
) -> float | None:
    """Median absolute elevation of the OBJECT_MSL fixtures inside the deck
    polygon (KBNA taxiway-L: twelve fixtures cluster at 166.9994 m)."""
    on_deck: list[float] = []
    for placement in mean_sea_level_placements:
        if placement.mean_sea_level_elevation_m is None:
            continue
        frame_x, frame_z = obj8_reader.lonlat_to_local_offset(
            origin_latitude,
            origin_longitude,
            0.0,
            placement.latitude,
            placement.longitude,
        )
        if deck_polygon.contains(Point((frame_x, frame_z))):
            on_deck.append(placement.mean_sea_level_elevation_m)
    if not on_deck:
        return None
    return median(on_deck)


# ---------------------------------------------------------------------------
# Feature C — structure ground interfaces (spec section 3.4, A5-A8, R10)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _BelowGradeHardEnclosure:
    """The interior-cutout trigger evidence (R10/A8): the below-grade
    drivable content, the at-grade plan footprint, and how much of the
    former the latter encloses."""

    enclosure_fraction: float
    below_grade_hard_union: object   # shapely geometry, frame metres
    at_grade_footprint: object       # shapely geometry, frame metres
    hard_content_minimum_y_m: float


def _below_grade_hard_enclosure(
    frame: _StructureFrame,
) -> _BelowGradeHardEnclosure | None:
    """Evidence for the R10 interior-cutout trigger, or ``None`` when the
    structure has no meaningful below-grade drivable content.

    The below-grade hard content is the same face set the tunnel signature
    tests (hard faces at or below :data:`TUNNEL_MIN_BODY_DEPTH_M`, area at
    least :data:`TUNNEL_MIN_BELOW_GRADE_DECK_AREA_M2`) — deliberately, so
    ENCLOSURE is the single discriminator between the two rules (A8: the
    same drivable-below-grade signature fires on EGLL tunnels and KDEN
    train halls; the decks are open at the mouths, the platforms are 100%
    enclosed).  The floor value is the HARD content's minimum corner y —
    never the deepest solid (the KDEN −19 m foundation-pile trap)."""
    below_grade_hard_faces = [
        triangle
        for triangle in frame.triangles
        if triangle.is_hard
        and triangle.horizontality >= NEAR_HORIZONTAL_NORMAL_Y_MIN
        and triangle.height_m <= -TUNNEL_MIN_BODY_DEPTH_M
    ]
    if (
        sum(face.area_m2 for face in below_grade_hard_faces)
        < TUNNEL_MIN_BELOW_GRADE_DECK_AREA_M2
    ):
        return None
    at_grade_faces = [
        triangle
        for triangle in frame.triangles
        if triangle.height_m >= -TUNNEL_ROOF_TOP_TOLERANCE_M
    ]
    below_union = _union_horizontal(
        below_grade_hard_faces, keep_all_parts=True
    )
    at_grade_footprint = _union_horizontal(
        at_grade_faces, close_m=AT_GRADE_FOOTPRINT_CLOSE_M, keep_all_parts=True
    )
    if below_union is None:
        return None
    hard_content_minimum_y_m = min(
        corner[1]
        for face in below_grade_hard_faces
        for corner in face.corners
    )
    if at_grade_footprint is None:
        enclosure_fraction = 0.0
    else:
        try:
            enclosed_area = below_union.intersection(at_grade_footprint).area
        except (ValueError, _GEOS_EXCEPTION):
            enclosed_area = 0.0
        enclosure_fraction = (
            enclosed_area / below_union.area if below_union.area > 0 else 0.0
        )
    return _BelowGradeHardEnclosure(
        enclosure_fraction=enclosure_fraction,
        below_grade_hard_union=below_union,
        at_grade_footprint=at_grade_footprint,
        hard_content_minimum_y_m=hard_content_minimum_y_m,
    )


def _cluster_interface_levels(
    sector_low_envelopes: dict[int, float],
    wall_column_bases: Sequence[tuple[float, frozenset[str]]],
    dominant_area_resource: str | None,
) -> list[tuple[float, tuple[int, ...], float]]:
    """Cluster per-sector low envelopes into interface levels (A5) with
    the A7 dominant-area exception.

    ``sector_low_envelopes`` maps occupied sector index → low-envelope y.
    Levels are clustered at :data:`INTERFACE_LEVEL_CLUSTER_M`; a level
    below :data:`INTERFACE_LEVEL_MIN_PERIMETER_SHARE` of the occupied
    sectors is dropped as a parasite — EXCEPT a below-grade level carried
    by the dominant-area object (its wall columns include a base within
    the cluster band), which must survive (A7: the T1 main floor died to
    this filter before the exception).  Returns ``(level_y_m,
    sector_indices, perimeter_share)`` tuples, deepest level first."""
    if not sector_low_envelopes:
        return []
    members_by_bucket: dict[int, list[int]] = {}
    for sector_index, low_envelope in sector_low_envelopes.items():
        bucket = round(low_envelope / INTERFACE_LEVEL_CLUSTER_M)
        members_by_bucket.setdefault(bucket, []).append(sector_index)
    occupied_sector_count = len(sector_low_envelopes)
    levels: list[tuple[float, tuple[int, ...], float]] = []
    for bucket, sector_indices in members_by_bucket.items():
        level_y_m = sum(
            sector_low_envelopes[index] for index in sector_indices
        ) / len(sector_indices)
        perimeter_share = len(sector_indices) / occupied_sector_count
        if perimeter_share < INTERFACE_LEVEL_MIN_PERIMETER_SHARE:
            dominant_carries_level = (
                dominant_area_resource is not None
                and level_y_m <= -INTERFACE_LEVEL_CLUSTER_M
                and any(
                    abs(base - level_y_m) <= INTERFACE_LEVEL_CLUSTER_M
                    and dominant_area_resource in resources
                    for base, resources in wall_column_bases
                )
            )
            if not dominant_carries_level:
                continue
        levels.append(
            (level_y_m, tuple(sorted(sector_indices)), perimeter_share)
        )
    levels.sort(key=lambda level: level[0])
    return levels


def _classify_structure_ground_interface(
    placements: Sequence[ObjectPlacement],
    frame: _StructureFrame,
    enclosure: _BelowGradeHardEnclosure | None,
) -> StructureGroundInterface | None:
    """Extract and classify one building structure's ground interface
    (spec section 3.4).  Returns ``None`` for structures with no wall
    geometry and no below-grade drivable content (ground clutter)."""
    # Wall columns (A5): vertical extent filters out roof overhangs and
    # decals, whose edges otherwise dominate the base profile.
    wall_columns: list[tuple[float, float, float, frozenset[str]]] = []
    # (frame x, frame z, base y, resources)
    for (grid_x, grid_z), (
        minimum_y,
        maximum_y,
        resources,
    ) in frame.vertex_columns.items():
        if maximum_y - minimum_y >= WALL_COLUMN_MIN_VERTICAL_EXTENT_M:
            wall_columns.append(
                (
                    grid_x * WALL_COLUMN_GRID_M,
                    grid_z * WALL_COLUMN_GRID_M,
                    minimum_y,
                    resources,
                )
            )
    cutout_triggered = (
        enclosure is not None
        and enclosure.enclosure_fraction
        >= INTERIOR_CUTOUT_ENCLOSURE_MIN_FRACTION
    )
    if not wall_columns and not cutout_triggered:
        return None

    # Sector frame: angles around the wall-column centroid (or the face
    # centroid when a cutout fires on a column-less structure).
    if wall_columns:
        centroid_x = sum(column[0] for column in wall_columns) / len(
            wall_columns
        )
        centroid_z = sum(column[1] for column in wall_columns) / len(
            wall_columns
        )
    else:
        total_area = sum(t.area_m2 for t in frame.triangles) or 1.0
        centroid_x = (
            sum(t.centroid_xz[0] * t.area_m2 for t in frame.triangles)
            / total_area
        )
        centroid_z = (
            sum(t.centroid_xz[1] * t.area_m2 for t in frame.triangles)
            / total_area
        )

    def _sector_of(x: float, z: float) -> int:
        angle = math.atan2(z - centroid_z, x - centroid_x)
        sector = int(
            (angle + math.pi) / (2.0 * math.pi) * PERIMETER_SECTOR_COUNT
        )
        return min(sector, PERIMETER_SECTOR_COUNT - 1)

    bases_by_sector: dict[int, list[float]] = {}
    for column_x, column_z, base_y, _resources in wall_columns:
        bases_by_sector.setdefault(
            _sector_of(column_x, column_z), []
        ).append(base_y)
    sector_low_envelopes: dict[int, float] = {}
    for sector_index, bases in bases_by_sector.items():
        sector_low_envelopes[sector_index] = float(
            numpy.percentile(
                numpy.asarray(bases), FACADE_BASE_LOW_ENVELOPE_PERCENTILE
            )
        )

    # Dominant-area resource (A7 exception key): by solid face area.
    area_by_resource: dict[str, float] = {}
    for triangle in frame.triangles:
        area_by_resource[triangle.resource_path] = (
            area_by_resource.get(triangle.resource_path, 0.0)
            + triangle.area_m2
        )
    dominant_area_resource = (
        max(area_by_resource, key=lambda key: area_by_resource[key])
        if area_by_resource
        else None
    )

    wall_column_bases = [
        (base_y, resources)
        for _x, _z, base_y, resources in wall_columns
    ]
    interface_levels = _cluster_interface_levels(
        sector_low_envelopes, wall_column_bases, dominant_area_resource
    )

    # Ground-contact fractions (work order round 4), by face area.
    total_face_area = sum(t.area_m2 for t in frame.triangles)
    area_by_sector = [0.0] * PERIMETER_SECTOR_COUNT
    contact_area_by_sector = [0.0] * PERIMETER_SECTOR_COUNT
    contact_area_total = 0.0
    for triangle in frame.triangles:
        sector_index = _sector_of(*triangle.centroid_xz)
        area_by_sector[sector_index] += triangle.area_m2
        if abs(triangle.height_m) <= GROUND_CONTACT_BAND_HALF_WIDTH_M:
            contact_area_by_sector[sector_index] += triangle.area_m2
            contact_area_total += triangle.area_m2
    ground_contact_fraction = (
        contact_area_total / total_face_area if total_face_area > 0 else 0.0
    )
    ground_contact_fraction_by_sector = [
        (
            contact_area_by_sector[index] / area_by_sector[index]
            if area_by_sector[index] > 0
            else 0.0
        )
        for index in range(PERIMETER_SECTOR_COUNT)
    ]

    # Elevated deck/road above the footprint: confirms a bowl (T1 helix),
    # is a decoy over a flat structure (ELLX roadway) — recorded, never
    # deciding (A7).
    elevated_deck_area = sum(
        triangle.area_m2
        for triangle in frame.triangles
        if triangle.horizontality >= NEAR_HORIZONTAL_NORMAL_Y_MIN
        and triangle.height_m >= BRIDGE_DECK_CARRIED_MIN_HEIGHT_M
    )
    elevated_deck_above = elevated_deck_area >= BRIDGE_MIN_DECK_AREA_M2

    structure_footprint = _union_horizontal(
        frame.triangles,
        close_m=AT_GRADE_FOOTPRINT_CLOSE_M,
        keep_all_parts=True,
    )
    structure_footprint_area = (
        structure_footprint.area if structure_footprint is not None else 0.0
    )

    # --- classification, in evidence order ---------------------------------
    interface_class = INTERFACE_FLAT_CONFIRMED
    below_grade_footprint = None
    floor_y_m: float | None = None
    floor_is_bound_not_target = False

    # The bowl key (A7, as measured — see BOWL_MAX_AT_GRADE_BASE_SHARE):
    # share of wall columns based within the ground band.
    at_grade_column_count = sum(
        1
        for base_y, _resources in wall_column_bases
        if abs(base_y) <= GROUND_CONTACT_BAND_HALF_WIDTH_M
    )
    at_grade_wall_base_share = (
        at_grade_column_count / len(wall_column_bases)
        if wall_column_bases
        else 0.0
    )

    # Below-grade interface levels, deepest first (list is sorted).
    below_grade_levels = [
        level
        for level in interface_levels
        if level[0] <= -BOWL_MIN_BELOW_GRADE_LEVEL_DEPTH_M
    ]
    # The bowl floor bound is the largest-share below-grade level — the
    # shell base (T1: −3.42 at 39% share), not the deepest stray column.
    bowl_floor_level = (
        max(below_grade_levels, key=lambda level: level[2])
        if below_grade_levels
        else None
    )

    trench_level: tuple[float, tuple[int, ...], float] | None = None
    trench_footprint = None
    for level_y_m, sector_indices, perimeter_share in interface_levels:
        if level_y_m > -TRENCH_SPINE_MIN_DEPTH_M:
            continue
        if perimeter_share < TRENCH_SPINE_MIN_LEVEL_PERIMETER_SHARE:
            continue
        contributing_resources = {
            resource
            for base_y, resources in wall_column_bases
            if abs(base_y - level_y_m) <= INTERFACE_LEVEL_CLUSTER_M
            for resource in resources
        }
        if (
            len(contributing_resources)
            < TRENCH_SPINE_MIN_CONTRIBUTING_OBJECTS
        ):
            continue
        candidate_footprint = _union_horizontal(
            [
                triangle
                for triangle in frame.triangles
                if triangle.height_m <= -TRENCH_SPINE_MIN_DEPTH_M
            ],
            close_m=AT_GRADE_FOOTPRINT_CLOSE_M,
            keep_all_parts=True,
        )
        if candidate_footprint is None:
            continue
        # Largest CONNECTED part, never the sum: a coherent corridor is
        # the trench signature; scattered below-grade specks (EGLL jetway
        # slack) summed past the floor in the round-5 full-pack run.
        candidate_parts = (
            list(candidate_footprint.geoms)
            if candidate_footprint.geom_type == "MultiPolygon"
            else [candidate_footprint]
        )
        largest_part = max(candidate_parts, key=lambda part: part.area)
        if largest_part.area < TRENCH_SPINE_MIN_FOOTPRINT_AREA_M2:
            continue
        trench_level = (level_y_m, sector_indices, perimeter_share)
        trench_footprint = largest_part
        break  # levels are sorted deepest first

    if cutout_triggered:
        interface_class = INTERFACE_INTERIOR_CUTOUT
        try:
            below_grade_footprint = (
                enclosure.below_grade_hard_union.intersection(
                    enclosure.at_grade_footprint
                )
                if enclosure.at_grade_footprint is not None
                else enclosure.below_grade_hard_union
            )
        except (ValueError, _GEOS_EXCEPTION):
            below_grade_footprint = enclosure.below_grade_hard_union
        floor_y_m = enclosure.hard_content_minimum_y_m
    elif (
        ground_contact_fraction <= BOWL_MAX_GROUND_CONTACT_FRACTION
        and at_grade_wall_base_share <= BOWL_MAX_AT_GRADE_BASE_SHARE
        and bowl_floor_level is not None
        and structure_footprint_area
        >= STRUCTURE_INTERFACE_MIN_FOOTPRINT_AREA_M2
    ):
        interface_class = INTERFACE_BOWL_UNDER_DECK
        below_grade_footprint = structure_footprint
        # Objects under-specify bowl depth (A7: T1 shell base −3.4 m where
        # the reference hand patch cuts −8 m) — a BOUND, never a target.
        floor_y_m = bowl_floor_level[0]
        floor_is_bound_not_target = True
    elif (
        trench_level is not None
        and structure_footprint_area
        >= STRUCTURE_INTERFACE_MIN_FOOTPRINT_AREA_M2
    ):
        interface_class = INTERFACE_TRENCH_SPINE
        below_grade_footprint = trench_footprint
        floor_y_m = trench_level[0]

    perimeter_base_profile = [
        (
            (sector_index + 0.5) * 360.0 / PERIMETER_SECTOR_COUNT,
            sector_low_envelopes[sector_index],
        )
        for sector_index in sorted(sector_low_envelopes)
    ]

    reference_placement = placements[0]
    return StructureGroundInterface(
        object_resources=sorted(
            {placement.resource_path for placement in placements}
        ),
        anchor_longitude_latitude=(
            reference_placement.longitude,
            reference_placement.latitude,
        ),
        frame_origin_longitude_latitude=(
            frame.origin_longitude,
            frame.origin_latitude,
        ),
        heading_degrees=reference_placement.heading_degrees,
        perimeter_base_profile=perimeter_base_profile,
        interface_levels=interface_levels,
        split_level=len(interface_levels) > 1,
        ground_contact_fraction=ground_contact_fraction,
        ground_contact_fraction_by_sector=ground_contact_fraction_by_sector,
        at_grade_wall_base_share=at_grade_wall_base_share,
        interface_class=interface_class,
        below_grade_footprint=below_grade_footprint,
        floor_y_m=floor_y_m,
        floor_is_bound_not_target=floor_is_bound_not_target,
        elevated_deck_above=elevated_deck_above,
    )


# ---------------------------------------------------------------------------
# Round-5 mega-pool component refinement
# ---------------------------------------------------------------------------

def _per_resource_face_footprints(
    triangles: Sequence[_FrameTriangle],
    face_predicate,
) -> dict[str, object]:
    """Union footprint of the faces passing ``face_predicate``, per
    resource.  Resources with no passing face are absent."""
    polygons_by_resource: dict[str, list] = {}
    for triangle in triangles:
        if face_predicate(triangle):
            polygons_by_resource.setdefault(
                triangle.resource_path, []
            ).append(triangle.horizontal_polygon)
    footprints: dict[str, object] = {}
    for resource, polygons in polygons_by_resource.items():
        try:
            union = unary_union(polygons)
            if not union.is_valid:
                union = union.buffer(0)
        except (ValueError, _GEOS_EXCEPTION):
            continue
        if not union.is_empty:
            footprints[resource] = union
    return footprints


def _footprint_components(
    footprint_by_resource: dict[str, object],
    join_buffer_m: float,
) -> list[set[str]]:
    """Union-find components over resources whose footprints come within
    ``join_buffer_m`` of each other."""
    resources = sorted(footprint_by_resource)
    parent = list(range(len(resources)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[left_root] = right_root

    buffered = {}
    for resource in resources:
        try:
            buffered[resource] = footprint_by_resource[resource].buffer(
                join_buffer_m
            )
        except (ValueError, _GEOS_EXCEPTION):
            buffered[resource] = footprint_by_resource[resource]
    for first_index in range(len(resources)):
        for second_index in range(first_index + 1, len(resources)):
            try:
                touches = buffered[resources[first_index]].intersects(
                    footprint_by_resource[resources[second_index]]
                )
            except (ValueError, _GEOS_EXCEPTION):
                touches = True  # doubt merges, never tears (I-20 spirit)
            if touches:
                union(first_index, second_index)

    members_by_root: dict[int, set[str]] = {}
    for index, resource in enumerate(resources):
        members_by_root.setdefault(find(index), set()).add(resource)
    return list(members_by_root.values())


def _below_grade_drivable_components(
    placements: Sequence[ObjectPlacement],
    frame: _StructureFrame,
) -> list[set[str]]:
    """Tunnel/interior-cutout candidate components inside one pool.

    Seeds: resources owning near-horizontal HARD faces below
    :data:`TUNNEL_MIN_BODY_DEPTH_M` (their below-grade deck footprints),
    plus resources placed with a below-grade ``OBJECT_AGL`` offset (whole
    footprint — the EGLL AGL shells carry no hard).  Non-seed resources
    are attached when at least
    :data:`TUNNEL_COVER_CONTAINMENT_MIN_FRACTION` of their own footprint
    lies over the component's seed footprint — the roof shell over its
    deck — so mouths (deck − roof) still compute per tunnel."""
    below_grade_agl_resources = _agl_tunnel_seed_resources(
        placements, frame.triangles
    )
    seed_footprints = _per_resource_face_footprints(
        frame.triangles,
        lambda triangle: (
            triangle.is_hard
            and triangle.horizontality >= NEAR_HORIZONTAL_NORMAL_Y_MIN
            and triangle.height_m <= -TUNNEL_MIN_BODY_DEPTH_M
        )
        or triangle.resource_path in below_grade_agl_resources,
    )
    if not seed_footprints:
        return []
    components = _footprint_components(
        seed_footprints, TUNNEL_COMPONENT_JOIN_BUFFER_M
    )

    # Attach cover (roof shell) resources.
    full_footprints = _per_resource_face_footprints(
        frame.triangles, lambda triangle: True
    )
    attached_components: list[set[str]] = []
    for component in components:
        try:
            component_footprint = unary_union(
                [seed_footprints[resource] for resource in component]
            ).buffer(TUNNEL_COMPONENT_JOIN_BUFFER_M)
        except (ValueError, _GEOS_EXCEPTION):
            attached_components.append(component)
            continue
        attached = set(component)
        for resource, footprint in full_footprints.items():
            if resource in attached or footprint.area <= 0.0:
                continue
            try:
                contained = footprint.intersection(component_footprint).area
            except (ValueError, _GEOS_EXCEPTION):
                continue
            if (
                contained / footprint.area
                >= TUNNEL_COVER_CONTAINMENT_MIN_FRACTION
            ):
                attached.add(resource)
        attached_components.append(attached)
    return attached_components


def _hard_face_components(frame: _StructureFrame) -> list[set[str]]:
    """Bridge candidate components: resources owning near-horizontal hard
    faces, grouped by footprint adjacency
    (:data:`BRIDGE_COMPONENT_JOIN_BUFFER_M`)."""
    seed_footprints = _per_resource_face_footprints(
        frame.triangles,
        lambda triangle: (
            triangle.is_hard
            and triangle.horizontality >= NEAR_HORIZONTAL_NORMAL_Y_MIN
        ),
    )
    if not seed_footprints:
        return []
    return _footprint_components(
        seed_footprints, BRIDGE_COMPONENT_JOIN_BUFFER_M
    )


def _bridge_evidence_resources(
    component: set[str],
    frame: _StructureFrame,
) -> set[str]:
    """The component plus every pool resource whose footprint intersects
    the component's hard footprint buffered by the abutment search radius
    — the grounding cladding the per-end test must see (EDDF's Tunnel_N
    trench walls belong to their Bridge_N deck)."""
    component_hard = _per_resource_face_footprints(
        frame.triangles,
        lambda triangle: (
            triangle.resource_path in component
            and triangle.is_hard
            and triangle.horizontality >= NEAR_HORIZONTAL_NORMAL_Y_MIN
        ),
    )
    if not component_hard:
        return set(component)
    try:
        buffered = unary_union(list(component_hard.values())).buffer(
            ABUTMENT_GRADE_SEARCH_RADIUS_M
        )
    except (ValueError, _GEOS_EXCEPTION):
        return set(component)
    evidence = set(component)
    full_footprints = _per_resource_face_footprints(
        frame.triangles, lambda triangle: True
    )
    for resource, footprint in full_footprints.items():
        if resource in evidence:
            continue
        try:
            if footprint.intersects(buffered):
                evidence.add(resource)
        except (ValueError, _GEOS_EXCEPTION):
            evidence.add(resource)
    return evidence


def _wall_column_count(frame: _StructureFrame) -> int:
    return sum(
        1
        for minimum_y, maximum_y, _resources in frame.vertex_columns.values()
        if maximum_y - minimum_y >= WALL_COLUMN_MIN_VERTICAL_EXTENT_M
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _pavement_union_in_frame(
    pavement_polygons_longitude_latitude,
    origin_latitude: float,
    origin_longitude: float,
) -> Polygon | None:
    """Project caller-supplied lon/lat pavement polygons into the structure
    frame and union them (for the contract coverage test).

    Accepts shapely Polygons, MultiPolygons (a self-crossing draped ring
    repaired by ``buffer(0)`` upstream arrives as one — real KBNA input),
    or raw ``(longitude, latitude)`` rings."""
    if not pavement_polygons_longitude_latitude:
        return None
    exterior_rings: list[list[tuple[float, float]]] = []
    for polygon in pavement_polygons_longitude_latitude:
        geometry_type = getattr(polygon, "geom_type", None)
        if geometry_type == "MultiPolygon":
            for part in polygon.geoms:
                exterior_rings.append(list(part.exterior.coords))
        elif geometry_type == "Polygon":
            exterior_rings.append(list(polygon.exterior.coords))
        else:
            exterior_rings.append(list(polygon))
    frame_polygons = []
    for ring in exterior_rings:
        frame_ring = []
        for longitude, latitude in ring:
            frame_x, frame_z = obj8_reader.lonlat_to_local_offset(
                origin_latitude, origin_longitude, 0.0, latitude, longitude
            )
            frame_ring.append((frame_x, frame_z))
        if len(frame_ring) < 3:
            continue
        candidate = Polygon(frame_ring)
        if not candidate.is_valid:
            candidate = candidate.buffer(0)
        if not candidate.is_empty:
            frame_polygons.append(candidate)
    if not frame_polygons:
        return None
    try:
        union = unary_union(frame_polygons)
    except (ValueError, _GEOS_EXCEPTION):
        return None
    return None if union.is_empty else union


def classify_object_terrain_features(
    placements: Sequence[ObjectPlacement],
    geometry_by_resource: dict[str, ObjectGeometry],
    *,
    pavement_polygons_longitude_latitude=None,
    mean_sea_level_placements: Sequence[ObjectPlacement] | None = None,
    pack_root: str = "",
    epsilon_metres: float = STRUCTURE_GROUPING_EPSILON_M,
) -> ClassificationResult:
    """Classify tunnels and bridges from placements and per-object geometry.

    Pure: nothing is read from disk here.  ``placements`` are the
    tunnel/bridge candidate placements the caller wishes to consider (a
    thin caller can pass every placement — a pool that matches no signature
    is simply ignored).  ``geometry_by_resource`` maps each placement's
    ``resource_path`` to its :class:`ObjectGeometry`.  Optional
    ``pavement_polygons_longitude_latitude`` (shapely polygons or ``(lon,
    lat)`` rings) drives the bridge terrain contract; ``mean_sea_level_
    placements`` (read upstream with ``include_object_msl=True``) supply
    absolute deck elevations.  ``pack_root`` is paired with each consumed
    resource in :attr:`ClassificationResult.exclusions` (ruling R4).

    Grouping reuses ``object_anchor.discover_object_pools``; each pool is
    tried as a tunnel first (below-grade signature) then as a bridge."""
    mean_sea_level_placements = mean_sea_level_placements or []
    resolved_paths = {
        placement.resource_path: placement.resource_path
        for placement in placements
        if placement.resource_path in geometry_by_resource
    }
    pools = discover_object_pools(
        list(placements),
        resolved_paths,
        geometry_by_resource,
        epsilon_metres=epsilon_metres,
    )

    tunnels: list[TunnelStructure] = []
    bridges: list[BridgeStructure] = []
    exclusions: list[tuple[str, str]] = []
    refusals: list[RefusedStructure] = []
    ground_interfaces: list[StructureGroundInterface] = []

    for pool in pools:
        frame = _build_structure_frame(pool.placements, geometry_by_resource)
        if not frame.triangles:
            continue
        consumed_resources: set[str] = set()

        # --- stage 1: below-grade drivable components (round 5) ---------
        # Tunnels and interior cutouts are classified per contributing
        # component, never per pool: mega-pools diluted every tunnel
        # metric and ballooned the R4 exclusion list (812 at EGLL).
        # Within a component, R10/A8 precedence holds: ENCLOSURE
        # discriminates the interior cutout from the tunnel.
        for component in _below_grade_drivable_components(
            pool.placements, frame
        ):
            component_placements = [
                placement
                for placement in pool.placements
                if placement.resource_path in component
            ]
            if not component_placements:
                continue
            component_frame = _build_structure_frame(
                component_placements, geometry_by_resource
            )
            if not component_frame.triangles:
                continue
            component_enclosure = _below_grade_hard_enclosure(
                component_frame
            )
            if (
                component_enclosure is not None
                and component_enclosure.enclosure_fraction
                >= INTERIOR_CUTOUT_ENCLOSURE_MIN_FRACTION
            ):
                ground_interface = _classify_structure_ground_interface(
                    component_placements,
                    component_frame,
                    component_enclosure,
                )
                if ground_interface is not None:
                    ground_interfaces.append(ground_interface)
                    consumed_resources |= component
                    for resource in ground_interface.object_resources:
                        exclusions.append((pack_root, resource))
                continue
            if _is_tunnel_signature(
                component_placements, component_frame.triangles
            ):
                tunnel = _classify_tunnel(
                    component_placements,
                    component_frame.origin_latitude,
                    component_frame.origin_longitude,
                    component_frame.triangles,
                )
                tunnels.append(tunnel)
                consumed_resources |= component
                for resource in tunnel.object_resources:
                    exclusions.append((pack_root, resource))

        remaining_placements = [
            placement
            for placement in pool.placements
            if placement.resource_path not in consumed_resources
        ]
        if not remaining_placements:
            continue
        remaining_frame = (
            frame
            if not consumed_resources
            else _build_structure_frame(
                remaining_placements, geometry_by_resource
            )
        )
        if not remaining_frame.triangles:
            continue

        # --- stage 2: bridge components ----------------------------------
        # Each hard-face component is tried separately (a mega-pool can
        # hold several bridges).  The evidence sub-frame adds the nearby
        # grounding cladding; the building-likeness gate applies per
        # EVIDENCE set, so a terminal's own drivable roadway (ELLX) routes
        # to feature C while a freestanding bridge next to clutter
        # (KBNA Crossing_Bridge) is classified — never silently absent.
        pavement_frame_union = _pavement_union_in_frame(
            pavement_polygons_longitude_latitude,
            remaining_frame.origin_latitude,
            remaining_frame.origin_longitude,
        )
        bridge_components = _hard_face_components(remaining_frame)
        for component in bridge_components:
            evidence_resources = _bridge_evidence_resources(
                component, remaining_frame
            )
            evidence_placements = [
                placement
                for placement in remaining_placements
                if placement.resource_path in evidence_resources
            ]
            if not evidence_placements:
                continue
            evidence_frame = _build_structure_frame(
                evidence_placements, geometry_by_resource
            )
            if not evidence_frame.triangles:
                continue
            if (
                _wall_column_count(evidence_frame)
                >= BUILDING_MIN_WALL_COLUMN_COUNT
            ):
                # Building-carried drivable surface: feature C's domain
                # (the pool remainder below emits the interface record).
                continue
            bridge, refusal_reason = _classify_bridge(
                evidence_placements,
                evidence_frame,
                pavement_frame_union,
                mean_sea_level_placements,
            )
            if bridge is not None:
                bridges.append(bridge)
                consumed_resources |= set(bridge.object_resources)
                for resource in bridge.object_resources:
                    exclusions.append((pack_root, resource))
            elif refusal_reason is not None:
                refusals.append(
                    RefusedStructure(
                        object_resources=sorted(component),
                        reason=refusal_reason,
                    )
                )
                consumed_resources |= component

        # Cosmetic bridges carry no hard faces at all (Murfreesboro):
        # when the remaining pool has no hard components and is not a
        # building, the whole-pool cosmetic path still applies.
        if not bridge_components and (
            _wall_column_count(remaining_frame)
            < BUILDING_MIN_WALL_COLUMN_COUNT
        ):
            bridge, refusal_reason = _classify_bridge(
                remaining_placements,
                remaining_frame,
                pavement_frame_union,
                mean_sea_level_placements,
            )
            if bridge is not None:
                bridges.append(bridge)
                consumed_resources |= set(bridge.object_resources)
                for resource in bridge.object_resources:
                    exclusions.append((pack_root, resource))
            elif refusal_reason is not None:
                refusals.append(
                    RefusedStructure(
                        object_resources=sorted(
                            {
                                placement.resource_path
                                for placement in remaining_placements
                            }
                        ),
                        reason=refusal_reason,
                    )
                )
                consumed_resources |= {
                    placement.resource_path
                    for placement in remaining_placements
                }

        # --- stage 3: feature C on what remains --------------------------
        building_placements = [
            placement
            for placement in pool.placements
            if placement.resource_path not in consumed_resources
        ]
        if not building_placements:
            continue
        building_frame = (
            remaining_frame
            if len(building_placements) == len(remaining_placements)
            else _build_structure_frame(
                building_placements, geometry_by_resource
            )
        )
        if not building_frame.triangles:
            continue
        ground_interface = _classify_structure_ground_interface(
            building_placements,
            building_frame,
            _below_grade_hard_enclosure(building_frame),
        )
        if ground_interface is not None:
            ground_interfaces.append(ground_interface)
            if ground_interface.interface_class != INTERFACE_FLAT_CONFIRMED:
                # Split-level structures whose terrain is adapted to them
                # join the R4 exclusion list exactly like tunnels (§3.4);
                # FLAT_CONFIRMED adapts nothing and stays bakeable.
                for resource in ground_interface.object_resources:
                    exclusions.append((pack_root, resource))

    return ClassificationResult(
        tunnels=tunnels,
        bridges=bridges,
        exclusions=exclusions,
        refusals=refusals,
        ground_interfaces=ground_interfaces,
    )
