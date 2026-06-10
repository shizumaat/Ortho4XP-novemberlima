"""Pavement-builder configuration constants.

Single source of truth for every numeric tunable in the airport
pavement builder.  Module-local constants in other O4_Pavement_*
modules should be reserved for values whose meaning is genuinely
specific to that module; anything tuned across the pipeline lives
here so reviewers can audit the whole tuning surface in one place.

For user-facing knobs (GUI / cfg-file persistence) register the
variable in O4_Cfg_Vars.py instead.
"""

__all__ = [
    "LOG_VERBOSITY",
    "AXIS_ALIGN_TOL_DEG",
    "LOAD_DSF_PAVEMENT",
    "SLOPING_EDGE_SNAP_M",
    "EMIT_JUNCTIONS",
    "EMIT_APRONS",
    "ENABLE_SERVICE_ROADS",
    "ABSORB_RECTS_ALONGSIDE_APRONS",
    "ENABLE_DISCOVERED_TAXIWAYS",
    "ENABLE_APRON_NECK_SPLIT",
    "HOLE_ROUTER_ENABLED",
    "HOLE_ROUTER_V2",
    "EMIT_BRIDGES_AND_TUNNELS",
    "JUNCTION_CLUSTER_DIST_M",
    "MAX_BOUNDARY_EDGE_M",
    "MIN_SEGMENT_LEN_M",
    "NECK_ABSOLUTE_M",
    "NECK_ABSORB_FRAC",
    "NECK_RELATIVE",
    "ROLE_GRADE_LIMITS",
    "TAXI_MAX_GRADE",
    "APRON_MAX_GRADE",
    "TERMINAL_MAX_GRADE",
    "TERMINAL_PADS_SLOPE",
    "TAXI_CORRIDOR_PROFILE",
    "TAXIWAY_MAX_GRADE_CHANGE_PER_M",
    "SERVICE_ROAD_MAX_GRADE",
    "SERVICE_ROAD_WIDTH_M",
    "MIN_SERVICE_STRIP_LEN_M",
    "OSM_SMALL_ROAD_HIGHWAY_TYPES",
    "SERVICE_ROAD_PAVEMENT_NEAR_M",
    "RUNWAY_MAX_GRADE",
    "RUNWAY_END_GRADE",
    "RUNWAY_END_FRACTION",
    "TUNNEL_RAMP_MAX_GRADE",
    "GROUNDSIDE_MAX_GRADE",
    "RUNWAY_VERTICAL_CURVE_K_M",
    "RUNWAY_MAX_GRADE_CHANGE_PER_M",
    "RUNWAY_DEM_FOLLOW_BAND_M",
    "GRADE_VISIBILITY_BUFFER_M",
    "ELEV_ROUNDING_NOISE_M",
    "RUNWAY_ADJACENCY_TOL_M",
    "RUNWAY_BOUNDARY_TOL_M",
    "RUNWAY_INSIDE_APRON_FRAC",
    "RUNWAY_APRON_AREA_RATIO",
    "SLIVER_ANGLE_THRESHOLD_DEG",
    "PATCH_SLOPE_CELL_SIZE_M",
    "RUNWAY_CELL_SIZE_M",
    "PATCH_SLOPE_PROFILE",
    "CLEARANCE_OBSTRUCTION_THRESHOLD_M",
    "CLEARANCE_MAX_REACH_M",
    "CLEARANCE_STATION_STEP_M",
    "RUNWAY_END_CLEARANCE_LENGTH_BY_CODE",
    "RUNWAY_END_RESA_MAX_SLOPE",
    "CLEARANCE_LATERAL_MAX_SLOPE",
    "RUNWAY_STRIP_HALF_WIDTH_BY_CODE",
    "WINGSPAN_BY_CODE_LETTER",
    "TAXIWAY_WINGTIP_MARGIN_M",
    "runway_code_number",
    "runway_strip_half_width_m",
    "runway_end_clearance_length_m",
    "taxiway_code_letter",
    "taxiway_clearance_half_width_m",
    "taxiway_clearance_half_width_for_letter",
]


# ── Build logging verbosity ─────────────────────────────────────
# Single knob for how chatty an auto-patch build is.  Auto-patch sets
# ``O4_UI_Utils.verbosity`` to this when it runs.  Build-time
# verification ALWAYS runs (it's how we surface "this airport has
# errors") — this only controls how much else is printed.
#   2 = debug  : every progress / diagnostic message.
#   1 = normal : per-airport progress + verification summary.
#   0 = critical: only verification PROBLEMS + errors — the default, so
#                 a normal Ortho4XP run's output window stays quiet
#                 except when an airport patch has issues.
LOG_VERBOSITY = 0


# ── Junction-refinement rule constants (user 2026-05-01) ─────────
# Plan: ``/Users/noah/.claude/plans/kind-meandering-sifakis.md``.

# Rule 2: junction vertex within this distance of a sloping-rect
# edge (any edge — sloping or cross — of a rect with a sloping role)
# gets snapped to the nearest rect corner.
# Per user 2026-05-04: bumped 10 m → 20 m to match the runway snap
# (RUNWAY_ADJACENCY_TOL_M).  10 m left vertices like SPJC junction
# -10153's v5 (17.88 m perpendicular to V3's edge) outside the snap
# radius, which forced the junction polygon to cut across V3 and
# produce a 1012 m² overlap.
SLOPING_EDGE_SNAP_M = 20.0

# Rule 4: a junction polygon is split at its narrowest cross-section
# when that thickness is below NECK_ABSOLUTE_M (in metres) OR is
# below NECK_RELATIVE × the polygon's MRR long-side length
# (whichever fires first per user 2026-05-01).
NECK_ABSOLUTE_M = 5.0
NECK_RELATIVE = 0.10
# A piece resulting from a neck split is absorbed into a neighbouring
# rect / junction / apron when the neighbour shares more than this
# fraction of the piece's perimeter.
NECK_ABSORB_FRAC = 0.70

# Rule 1: a junction vertex is "on the runway boundary" when within
# this distance of the runway polygon edge.  Used to identify the
# runway-adjacent vertex run that gets replaced with the runway's
# exact node sequence.
RUNWAY_BOUNDARY_TOL_M = 1.5

# Rule 1 v2 (user 2026-05-02): the WIDER tolerance for detecting
# vertices in a junction's runway-facing region.  Vertices in this
# band but outside RUNWAY_BOUNDARY_TOL_M still count as part of the
# junction's joining edge that needs to widen out to the next
# runway node.
#
# Bumped 5 m → 20 m per user 2026-05-04: any junction vertex within
# 20 m of the runway boundary should snap 1:1 to a runway segment
# corner — no extra nodes floating near the runway.  The 5 m band
# left vertices that had been pushed off the runway boundary by Rule
# 5 (1 m perp) plus densification (2 m boundary-trace noise) outside
# the snap radius; 20 m comfortably captures both.
RUNWAY_ADJACENCY_TOL_M = 20.0

# Rule 3 test tolerance: a non-pavement, non-anchor junction edge
# must run parallel or perpendicular to the longest runway axis
# within this many degrees.
AXIS_ALIGN_TOL_DEG = 2.0


# Max length of any junction-polygon ring segment.  Long edges get
# subdivided to anchor Triangle4XP's interior triangulation; without
# this the elevation solver leaves the interior un-anchored on long
# straight runs and produces visible cliffs.  Shared between the
# junction-decomposition pass (densification) and the elevation
# layer (vertex-aware grade clamp).
MAX_BOUNDARY_EDGE_M = 50.0


# Drop emitted line/segment fragments shorter than this length.
# Shared across centerline extraction, taxi-rect splitting, and the
# Phase-A apt.dat-rect chain construction.
MIN_SEGMENT_LEN_M = 15.0


# Cluster of junction-corner candidates: any two within this
# distance get merged when computing the residue's seam points.
JUNCTION_CLUSTER_DIST_M = 40.0

# Interior angles below this threshold count as "needle-tip"
# slivers.  Residue construction can leave thin wedges where
# rect / terminal edges meet the apt.dat boundary at near-collinear
# angles.  The polygon is shapely-valid but a sub-2 deg corner
# forces Triangle4XP to emit a near-degenerate triangle there --
# crashes X-Plane's mesh builder.  Caught at junction-emission
# time by _drop_sliver_corners (drops just the tip vertex), and
# again by a to_osm safety net (drops the whole shape if any
# slipped through).
SLIVER_ANGLE_THRESHOLD_DEG = 2.0


# Apron-merged runway detection: when at least this fraction of a
# runway-segment polygon lies inside an apt.dat / DSF apron polygon
# (and that polygon is much larger than the segment — see
# RUNWAY_APRON_AREA_RATIO), the segment is treated as apron-merged
# and the separate rect is dropped.
RUNWAY_INSIDE_APRON_FRAC = 0.95
# The containing apt.dat / DSF polygon must be >= this ratio times
# the segment area to count as an apron.
RUNWAY_APRON_AREA_RATIO = 3.0

# Bridge / tunnel emission flag.  Gates the four feature emit calls
# in build_airport_pavement: _emit_through_airport_depressed_roads,
# _emit_tunnel_portals, _emit_taxi_bridges,
# _emit_underpass_road_approaches.  Each carves its footprint out of
# overlapping airside / groundside pavement before emitting so
# ``test_no_self_overlap`` stays green.
EMIT_BRIDGES_AND_TUNNELS = True

# Through-airport depressed roads (user 2026-06-10): DISABLED for now —
# instead of depressing a road's entire inside-airport stretch to
# apt_elev−8 m (open trench), only the tunnel-portal ramps are built
# (the road descends at each portal and the tunnel-tagged stretch stays
# under the airport surface).  The pre-solve terminal-gap carve is
# gated on this too (no trench → no gap through the terminals).
EMIT_DEPRESSED_ROADS = False

# Combine apt.dat with DSF pavement polygons: when True the
# smart-apt.dat selector still runs to choose the best custom-pack
# vs global candidate by OSM coverage; DSF polygons supplement
# whichever apt.dat is picked.
LOAD_DSF_PAVEMENT = True

# Third-party DSF pavement descriptors (user 2026-06-10, KPHX south
# aprons): a third-party ``.pol`` is trusted as BASE pavement when its
# path contains one of these material descriptors — the common naming
# convention across scenery libraries (ZDP_Library/.../concrete/flat/
# Flat_New_Uniform.pol, MisterX_Library/Ground_Textures/Asphalt_2_
# Base.pol, …).  Per user: "asphalt" and "concrete" plus their French,
# German, Spanish, Italian and Portuguese equivalents.  Decorative /
# non-pavement uses of the same words (lines, markings, stains, …) are
# rejected by the skip-token list in ``dsf_reader``.
DSF_PAVEMENT_MATERIAL_TOKENS = (
    # English
    "asphalt", "concrete",
    # French (asphalte, béton)
    "asphalte", "beton", "béton",
    # German (Asphalt — same spelling — and Beton, covered above)
    # Spanish (asfalto, hormigón / concreto)
    "asfalto", "hormigon", "hormigón", "concreto",
    # Italian (asfalto — same as Spanish — and calcestruzzo)
    "calcestruzzo",
    # Portuguese (asfalto — covered — and betão / concreto — covered)
    "betao", "betão",
)


# ── Aerodrome longitudinal grade standards (single source of truth) ──
# Every grade / vertical-curve rule VALUE lives here so the whole tuning
# surface is auditable in one place; other modules import these rather
# than redefining literals.  See docs/STANDARDS.md for the citations.
# Values are rise/run (decimal: 0.015 = 1.5%).
#
# These stay separate named constants even where the value currently
# coincides (taxiway, apron and runway are all 1.5% today) because they
# trace to different standards and may diverge — e.g. EASA could tighten
# the runway cap without touching taxiways.
TAXI_MAX_GRADE = 0.015          # FAA AC 150/5300-13 taxiway-family
APRON_MAX_GRADE = 0.015         # apron / junction body, all directions (user 2026-05-07)
# Terminal pads.  0.0 = perfectly FLAT (the default — a terminal building sits on
# one floor altitude); the solver derives its flatness from this cap (cap 0 → the
# flat / rigid-pad code path).  Raise it (e.g. to APRON_MAX_GRADE) to let terminals
# GRADE like aprons — they then follow terrain within the cap through the same
# visibility-graph path as every other surface, no special case.
# Terminal pads are rigid FLAT by default in the SOLVER (a building sits on one
# floor); this value is the MAXIMUM grade a terminal MAY take when it cannot stay
# flat — a pad squeezed between a low and a high runway must SLOPE to stay in grade
# to both (user 2026-06-09: flatness yields to grade).  It is also the cap the
# grade VALIDATOR (tools/check_grade.py, via ROLE_GRADE_LIMITS) holds terminals to,
# so the test always checks whatever the config says for each role.
TERMINAL_MAX_GRADE = APRON_MAX_GRADE
# Let EVERY terminal pad slope (up to TERMINAL_MAX_GRADE) through the same
# visibility-graph path as aprons, instead of the rigid-flat default with
# squeezed-pad exceptions.  Evaluation state (user 2026-06-10): with the flex
# demand synthesis landing runways on the route-justified profiles (HECA 05C
# min 110.9, not the rejected 104.4 over-dip), the chain tension the over-dip
# used to absorb must drain into the terminals.  Set False to restore the
# rigid-flat default (squeezed pads still slope via the seed marking).
TERMINAL_PADS_SLOPE = True
# Taxi-corridor profile pass (user 2026-06-10): a chain of taxi rects that
# CONTINUES through junctions (same ref, or the best axis-aligned
# continuation - HECA's T through junction -10292, T4 into U) is re-profiled
# as ONE smooth 1-D line, exactly like a runway centerline: grade-capped,
# grade-CHANGE-capped, anchored at hard nodes / runway contacts / corridor
# termini, DEM lowest priority.  Without it the solver settles each shape
# DEM-near and a corridor legally V-notches at a junction (T read 111.7 ->
# 104.5 -> 105.0 -> 103.5 - flat-to-reversed through the junction where one
# steady ~1 % ramp exists).  The corridor's profile then anchors the final
# within-shape enforcement (neighbouring pavement conforms to it - taxi
# routes outrank aprons per the user's priority model).
# ⛔ DEFAULT OFF (s73-close): the pass delivers the corridor continuity
# (T monotone through junction -10292, T4 chained into U) but junctions
# crossed by TWO corridors need a TILTED-PLANE crossing model (both axes
# slope, the user's "roll and yaw near equal") and the corridor seeds
# need route-floor awareness (T's flat seed ignored the 05C-route demand
# entering via T4) — without those, adjacent band writes leave up to 64 %
# internal junction cliffs.  Those two pieces ARE the route-field model
# (STATUS #3).  s73-p5 BUILT route-band threading + the junction TWIST
# blend (+ disagreement guard, stub/wide-only cross-ref merges): the
# named corridors land (T monotone through -10292, T4+U ~2 % steady,
# #291 internal 64%→25%).  s73-p7 BUILT the JOINT corridor-network
# solve: chains coupled at shared junctions as one system (crossing
# equality stations, terminus-projection + mouth geodesic cap ties,
# damped consensus + feasibility-guarded freeze, anchor
# self-consistency, junction hard bands on true in-polygon geodesics,
# enforce band-exemption for corridor junctions) — CYXY gate-on 19→0
# green, HECA #217 → 0, #291 → 13.6 %.  ON for in-sim evaluation
# (user 2026-06-10).  Known gate-on residual: HECA's T4-wall route
# tension (freeze-skipped ties, `O4_CORRIDOR_DEBUG=1` prints them) —
# the runway-flex arbitration, not a corridor bug.  False restores the
# pre-corridor surfaces byte-identically.
TAXI_CORRIDOR_PROFILE = True
# Taxiway vertical-curve rate (rise/run change per metre) used by the
# corridor profile - the taxi sibling of RUNWAY_MAX_GRADE_CHANGE_PER_M
# (driver.py re-exports it as MAX_TAXIWAY_GRADE_CHANGE_PER_M).
TAXIWAY_MAX_GRADE_CHANGE_PER_M = 1.0 / 3000.0
SERVICE_ROAD_MAX_GRADE = 0.040  # ground-vehicle route (apt.dat 1206 + OSM small roads) — cars handle 4%
# Ground-vehicle 4%-grade ``service_road`` rect geometry (session 47).
SERVICE_ROAD_WIDTH_M = 6.0          # corridor width for a service-road rect
MIN_SERVICE_STRIP_LEN_M = 25.0      # min dedicated-strip length to emit a rect
# OSM small-road inputs: which highway= types count as drivable "small
# roads" (graded with car logic, 4%).  Inside the airport boundary + a
# small outside buffer.  Excludes major roads (motorway/trunk/primary/
# secondary) and non-car ways (footway/path/cycleway/steps/pedestrian).
OSM_SMALL_ROAD_HIGHWAY_TYPES = frozenset((
    "service", "unclassified", "residential", "living_street",
    "track", "road", "tertiary",
))
# OSM small roads are kept ONLY where they hug airfield pavement: within
# SERVICE_ROAD_PAVEMENT_NEAR_M of any apt.dat/DSF pavement.  This drops
# the deep-interior road grid of large airports (HECA's 28 km² boundary
# held ~852 service shapes otherwise) and keeps only the apron-access /
# crossing roads that join the pavement.  apt.dat 1206 truck routes are
# authoritative and kept unconstrained.
SERVICE_ROAD_PAVEMENT_NEAR_M = 25.0    # keep OSM roads within this of aircraft pavement
RUNWAY_MAX_GRADE = 0.015        # FAA AC 150/5300-13B runway longitudinal (ARC C-E)
RUNWAY_END_GRADE = 0.008        # EASA CS-ADR-DSN / ICAO Annex 14, first/last quarter (code 3/4)
RUNWAY_END_FRACTION = 0.25      # extent of each runway end zone (fraction of length)
TUNNEL_RAMP_MAX_GRADE = 0.040   # navigable ramp grade for tunnel portals (user 2026-05-08)
GROUNDSIDE_MAX_GRADE = 0.040    # groundside pavement ramp grade (user 2026-05-22)
# FAA vertical-curve rule L = K × |Δg|.  K = 305 m for ARC C/D (lighter
# A/B ≈ 76 m, heavy E ≈ 610 m).  ``RUNWAY_MAX_GRADE_CHANGE_PER_M`` is the
# segment-smoother's equivalent: a 1% grade change needs ~305 m of curve,
# i.e. ~1/30000 grade change per metre of pavement.
RUNWAY_VERTICAL_CURVE_K_M = 305.0
RUNWAY_MAX_GRADE_CHANGE_PER_M = 1.0 / 30000.0
# How far the runway profile may follow the raw DEM away from the linear
# baseline through its true anchors (CIFP thresholds, seams, runway crossings).
# 0 = "flat": the runway is the flattest profile its anchors permit and the DEM
# is ignored for the interior (user 2026-06-06).  The original "max DEM
# following" value was 5.0 m, which let mid-runway sections free-float up to 5 m
# off the baseline — e.g. CYXY 14R/32L dipping 4.5 m into a valley between the
# 14R threshold and the 02/20 crossing, which pulled the connecting junction low
# and made stub A 7.4%.  ``faa_joint_solve`` still enforces every grade/curvature
# cap regardless of this value.
RUNWAY_DEM_FOLLOW_BAND_M = 0.0

# Within-shape grade-audit geometry — the SINGLE SOURCE OF TRUTH shared by the
# runtime audit (``elevation._report_within_shape_violations``, the WARN shown
# in the Ortho4XP window) and the validator (``tools/check_grade.py``, what the
# test suite asserts), so the two never diverge:
#   * A within-shape grade constraint exists between any two MUTUALLY-VISIBLE
#     vertices — a pair whose straight chord stays inside the polygon (grown by
#     ``GRADE_VISIBILITY_BUFFER_M``) — at ANY distance.  Visibility (not
#     proximity) is the gate: the average slope between two visible vertices is
#     a real grade the aircraft experiences however far apart they are, while a
#     chord that cuts across a non-convex notch is a phantom path and excluded.
#     There is deliberately NO distance cap — matches the solver's uncapped
#     ``unified_jacobi._visible_grade_edges``.
#   * ``ELEV_ROUNDING_NOISE_M`` absorbs single-decimal (0.1 m) altitude rounding.
GRADE_VISIBILITY_BUFFER_M = 1.0
ELEV_ROUNDING_NOISE_M = 0.15


# Per-role within-shape grade limits (rise / run).  The validator in
# tools/check_grade.py uses this table to decide whether a vertex pair on
# a polygon's ring is in violation.  ``None`` means "skip the within-shape
# grade check for this role" — used for shapes that intentionally trace
# terrain (boundary outline, groundside curbside) or that are vertical
# structures (retaining walls).  Values reference the named caps above so
# there is a single source.
ROLE_GRADE_LIMITS = {
    # Taxiway-like surfaces — 1.5% along centerline (axis), tested
    # here as 1.5% between any pair of ring vertices since the
    # ring follows the axis closely.
    "runway":             RUNWAY_MAX_GRADE,
    "primary_parallel":   TAXI_MAX_GRADE,
    "secondary_parallel": TAXI_MAX_GRADE,
    "stub":               TAXI_MAX_GRADE,
    "cross_connector":    TAXI_MAX_GRADE,
    # Apron / junction — 1.5% all directions within the polygon
    # (per user 2026-05-07).
    "apron":              APRON_MAX_GRADE,
    "junction":           APRON_MAX_GRADE,
    # Terminals: 0 = flat (default).  Drives the flat-vs-graded code path —
    # see TERMINAL_MAX_GRADE.
    "terminal":           TERMINAL_MAX_GRADE,
    # Tunnel ramps descend from pavement elevation to the tunnel
    # floor; 4% is the navigable taxi grade for ramped portals
    # (per user 2026-05-08).
    "tunnel_ramp":        TUNNEL_RAMP_MAX_GRADE,
    # Ground-vehicle service roads (apt.dat 1206) grade along their
    # axis like a taxiway but at 4% — service vehicles handle steeper
    # terrain than aircraft (session 47).
    "service_road":       SERVICE_ROAD_MAX_GRADE,
    # Service-road network junctions (bends / intersections) — graded
    # all-direction at the same 4% car-logic cap as the rects.
    "service_junction":   SERVICE_ROAD_MAX_GRADE,
    # ── Skip-list (no grade enforcement) ─────────────────────────
    # Airport boundary is a footprint outline that traces real
    # terrain at 5 m vertex spacing.  No taxiable surface, no
    # grade rule applies.
    "boundary":           None,
    # Retaining walls are vertical 4-vertex polygons with 2 corners
    # at apt elev and 2 at tunnel-floor elev; the wall is vertical
    # by design.  Grade between the high and low corners is the
    # full step over a sub-metre run.
    "retaining_wall":     None,
    # Groundside pavement (cars / buildings, curbside / drop-off /
    # parking) follows the DEM but is graded like a ramp to ≤ 4 % slope
    # (user 2026-05-22) — same cap as tunnel ramps — so steep terrain is
    # smoothed to a navigable surface rather than tracing raw terrain.
    "groundside_pavement": GROUNDSIDE_MAX_GRADE,
    # Wingtip / RESA clearance cuts trace the cut terrain surface
    # (per-vertex node_altitudes computed directly against the DEM
    # and a ramped ceiling); like the boundary they carry no
    # within-shape grade rule.
    "taxiway_clearance":  None,
    "runway_clearance":   None,
}

# Phase-1 emit-suppression toggles (kept from the pre-refactor
# baseline; iteration aids that remain useful).
EMIT_JUNCTIONS = True
EMIT_APRONS = False

# Ground-vehicle service-road network — gated OFF (deferred feature).  The
# service_roads.py machinery stays in place, but the OSM small-road lookup
# and the apt.dat 1206 truck-edge parse are skipped while disabled so we
# don't waste cycles loading roads we won't use.  Flip to re-enable.
ENABLE_SERVICE_ROADS = False
# Absorb taxi rects that share a sloping edge with an apron/junction into
# that apron (the "junctions don't live on sloping rect edges" rule).
# ON.  Session 51 TESTED OFF (no-absorption / clean model = keep the taxilane
# rect, apron = pav_union − rects wraps it).  Result: 20 failed vs 12 with
# absorb ON — the suite ENCODES the absorption model.  Turning it off makes
# taxi rects sit alongside junctions/aprons, which directly violates
# no_long_edge_proximity / no_vertex_on_sloping_rect_flat_edge /
# rect_short_edges_connect / runway_node_sharing / neighbour_corners.  The
# clean no-absorption model is viable but requires REDEFINING those ~7
# invariant tests (deliberate decision, not done).  Kept ON pending that.
# NOTE (audit): if kept ON, `_absorb_rects_at_junction_perimeters` should
# identify sloping edges via `source_axis`, not the corner-order convention
# (mis-IDs 1 CYXY / 14 SPJC rects).
ABSORB_RECTS_ALONGSIDE_APRONS = False  # (session 51 experiment 2026-05-27)

# Synthesise taxi-rect centerlines for strip-shaped pavement that carries no
# apt.dat/OSM centerline (unreferenced taxiways — common at small/remote
# airports).  Detected on the raw pav_union and fed through the SAME
# _build_taxi_rects pass; the builder's long-edge-at-boundary + apron-interior
# gates ensure only strips with nothing along their sloping edge become rects.
# See pavement/discovered_taxiways.py.
ENABLE_DISCOVERED_TAXIWAYS = True

# Phase 2: split large apron/junction residue pieces at their narrow NECKS
# (taxi-width pinches / arm mouths) into convex pads joined by short
# connectors.  Keeps each apron all-pair surface convex and feeds the
# directional-solver pad/connector hierarchy.  See pavement/apron_necks.py.
ENABLE_APRON_NECK_SPLIT = True

# (session 61) Open residue holes with the in-pavement VISIBILITY-GRAPH router
# (pavement/hole_router.py) instead of the full-span centroid guillotine in
# `_decompose_polygon_with_holes`: routed two-bridge SPLIT cuts that bend
# around rects corner-to-corner, never plant a mid-edge node, and never shear a
# far corner.  Default OFF while A/B-validating on HECA; flip via env
# ``O4_HOLE_ROUTER=1`` for a single build.
import os as _os  # noqa: E402
HOLE_ROUTER_ENABLED = _os.environ.get("O4_HOLE_ROUTER", "1") == "1"

# (session 68) Conforming-cuts hole-router REDESIGN: plan ALL of a polygon's
# hole-opening cuts as a Prim-style MIN-SPANNING-FOREST on ONE shared
# visibility graph (each hole connects to the nearest point of the already-
# connected boundary network — exterior ring or a previously-opened hole —
# via its two shortest node-disjoint bridges).  Replaces the v1 per-hole
# independent two-bridge cuts whose Dijkstra exits all converged on a single
# exterior hub vertex, creating needle-thin (1–2°) wedge slices that the
# downstream sliver guards truncated or dropped → uncovered-source wedges
# (the HECA 670 m² fan gap).  ``O4_HOLE_ROUTER_V2=0`` restores the v1
# planner for A/B comparison.  Only consulted when HOLE_ROUTER_ENABLED.
HOLE_ROUTER_V2 = _os.environ.get("O4_HOLE_ROUTER_V2", "1") == "1"

# (session 63) Cut long taxi rects AND runway segments at interior terrain
# extrema (``split_long_rects_along_terrain`` + the runway peak/valley seams in
# ``pavement/runway_segments.py``).  DEFAULT OFF (user 2026-06-05): the extrema
# cuts split straight sections into many segments, and verified across
# HECA/CYXY/SPLP they are NOT needed for runway grade compliance — runways stay
# within grade (CYXY fully compliant) and the smooth vertical profile handles
# terrain undulation.  Set ``O4_SPLIT_LONG_RECTS=1`` to restore the cuts.
SPLIT_LONG_RECTS_ENABLED = _os.environ.get("O4_SPLIT_LONG_RECTS", "0") == "1"


# ── Patch mesh-density tuning (X-Plane load-time optimization) ─────────
# Ortho4XP cuts each SLOPED pavement way into ``cell_size``-metre cells
# (``cuts_long = way_length / cell_size``) and interpolates altitude with
# ``profile`` ("spline" or "plane").  This INTERNAL CUT GRID — not the
# patch's vertex count — drives the airport mesh's triangle count, and
# thus X-Plane load time (HECA measured: cell_size=2 m → +2.24 M
# triangles, 75% of the whole tile, 9m40s load vs 39s without the patch).
#
# A 4-corner sloping rect is a flat tilted PLANE, so a cell_size ≥ the
# way length yields ZERO internal cuts and renders the identical surface
# with a fraction of the triangles.  Runways differ: they carry a real
# FAA vertical profile (crests/sags), so coarsening them too far flattens
# that curve — hence a separate knob.
#
# To find the optimal compromise, sweep these and measure each build with
# ``tools/mesh_region_tris.py`` (triangle count) + the X-Plane load time.
# Historical default 2 m carried a "KBNA finding" note (smooth runway
# vertical transitions) — raise the runway value cautiously.
PATCH_SLOPE_CELL_SIZE_M = 10      # taxiway / apron / boundary sloped rects
RUNWAY_CELL_SIZE_M = 10           # runway segments (real vertical profile)
# Longitudinal interpolation curve for altitude_high/low rects in the
# X-Plane mesh builder.  "plane" = constant grade (linear); "spline" =
# 3x^2-2x^3 smoothstep (flat-tangent at both ends).  Per user 2026-05-23
# (multi-airport DEM analysis): spline is the best fit on only ~6/27
# runways and 3/41 taxiways and never by >0.1 m, and its flat-steep-flat
# shape adds a washboard to constant-grade segments (the taxiway-A2 sag).
# A real surface is a constant grade per segment, so "plane" is the
# correct default; long segments crossing a hill are SPLIT at terrain
# extrema instead (the solver grades each piece within the 1.5% cap, so
# the seam between two plane segments is a <3% — typically <1% — fold,
# not a visible bump).  Lateral clearance inherits this so it tracks.
PATCH_SLOPE_PROFILE = "plane"   # "plane" | "spline"


# ── Surface lateral / end clearance (wingtip + RESA) ──────────────
# Aircraft wingspans exceed the paved width of taxiways/runways, and
# the standards (FAA AC 150/5300-13 TOFA, ICAO Annex 14 graded
# strip / RESA) reserve a clear lateral band on each side and a
# graded area off each runway end.  The clearance pass
# (``clearance.emit_surface_clearance_cuts``) samples the DEM inside
# those bands and CUTS terrain that rises more than the threshold
# above the adjacent surface EDGE altitude down to a ramped ceiling,
# so a wingtip overhanging the pavement clears it.  Terrain BELOW
# the surface is left untouched (cut-only — we never fill).
#
# A terrain point is an "obstruction" when it rises more than this
# many metres above the adjacent surface edge altitude.  Keyed by
# surface family ("taxiway" | "runway").
CLEARANCE_OBSTRUCTION_THRESHOLD_M = {
    "taxiway": 1.0,
    "runway":  1.0,
}

# Safety cap (m) on how far a clearance band reaches outward from the
# pavement edge, bounding earthwork.  Must be >= the largest band we
# actually want: a code-4 runway-end RESA is 240 m, so the runway cap
# sits above that; taxiway wingtip bands are <= ~43 m.
CLEARANCE_MAX_REACH_M = {
    "taxiway": 100.0,
    "runway":  300.0,
}

# Vertex spacing (m) along a surface edge when sampling/building the
# clearance band.  Matches ``ELEVATION_GRID_STEP_M`` (5 m) so the cut
# resolves the same terrain detail the elevation solver does.
CLEARANCE_STATION_STEP_M = 5.0

# RESA / runway-end graded distance (m) beyond the runway end, by
# ICAO Annex 14 code number (derived from runway length).  ICAO RESA
# min is 90 m (240 m recommended for code 3/4); these defaults fold
# the strip-end portion in and stay conservative-but-tunable.
RUNWAY_END_CLEARANCE_LENGTH_BY_CODE = {1: 60.0, 2: 90.0, 3: 150.0, 4: 240.0}

# Maximum longitudinal slope (rise/run) of the graded runway-end safety
# area.  ICAO Annex 14 caps RESA longitudinal slopes at 5%; the RESA
# ramp rises from the runway-end pavement elevation at this slope and
# daylights where it meets natural ground, so an undershooting /
# overrunning aircraft meets a gentle slope rather than a wall.
RUNWAY_END_RESA_MAX_SLOPE = 0.05

# Transverse slope (rise/run) of the LATERAL clearance strip alongside
# a runway/taxiway.  These strips are FLAT shadows of the surface they
# protect: at each station the strip sits at the local pavement-edge
# altitude (so it follows the surface's longitudinal profile) and
# extends out level — an extension of the pavement, not a ramp.  Terrain
# is cut down to this surface level ONLY where the DEM rises above it
# within the protected (code-letter wingtip) width; everything at or
# below the surface is left untouched (cut-only).  This is intentionally
# 0: a non-zero lateral slope grades the band down to a SUB-surface ramp,
# which carves canyons wherever the pavement sits below its surroundings
# (cut into a hillside / solver-sunk).  RESA end-caps still ramp — see
# RUNWAY_END_RESA_MAX_SLOPE.
CLEARANCE_LATERAL_MAX_SLOPE = 0.0

# Lateral graded-strip half-width (m) from the runway centerline, by
# ICAO code number (Annex 14 graded portion of the runway strip).
RUNWAY_STRIP_HALF_WIDTH_BY_CODE = {1: 30.0, 2: 40.0, 3: 75.0, 4: 75.0}

# Maximum aircraft wingspan (m) per ICAO code letter (Annex 14).  The
# taxiway clearance band is based on the WINGTIP REACH — half the
# wingspan from the centerline plus a small margin — i.e. how far a
# wingtip overhangs, which is the terrain a wingtip could actually
# strike.  (The Table-3-1 "centre-line → object" distances — A 16.25 …
# F 57.5 — are far larger; they protect against objects/buildings and
# include lateral-deviation allowances, which over-grade terrain.)
WINGSPAN_BY_CODE_LETTER = {
    "A": 15.0, "B": 24.0, "C": 36.0, "D": 52.0, "E": 65.0, "F": 80.0,
}

# Margin (m) added beyond the wingtip (FAA-style wingtip clearance).
TAXIWAY_WINGTIP_MARGIN_M = 3.0


def runway_code_number(length_m: float) -> int:
    """ICAO Annex 14 aerodrome reference code NUMBER from runway
    length: 1 (<800 m), 2 (800–1199), 3 (1200–1799), 4 (≥1800)."""
    if length_m >= 1800.0:
        return 4
    if length_m >= 1200.0:
        return 3
    if length_m >= 800.0:
        return 2
    return 1


def runway_strip_half_width_m(length_m: float) -> float:
    """Graded runway-strip half-width (m) from the centerline."""
    return RUNWAY_STRIP_HALF_WIDTH_BY_CODE[runway_code_number(length_m)]


def runway_end_clearance_length_m(length_m: float) -> float:
    """RESA / runway-end graded distance (m) beyond the runway end."""
    return RUNWAY_END_CLEARANCE_LENGTH_BY_CODE[runway_code_number(length_m)]


def taxiway_code_letter(width_m: float) -> str:
    """ICAO code LETTER inferred from taxiway pavement width (m).
    Widths: A 7.5, B 10.5, C 15/18, D 18/23, E 23, F 25 m."""
    if width_m >= 25.0:
        return "F"
    if width_m >= 23.0:
        return "E"
    if width_m >= 18.0:
        return "D"
    if width_m >= 15.0:
        return "C"
    if width_m >= 10.5:
        return "B"
    return "A"


def taxiway_clearance_half_width_for_letter(letter: str) -> float:
    """Taxiway clearance half-width (m) from the centerline for a given
    ICAO code LETTER = wingtip reach (½ max wingspan) + margin."""
    return (0.5 * WINGSPAN_BY_CODE_LETTER[letter.upper()]
            + TAXIWAY_WINGTIP_MARGIN_M)


def taxiway_clearance_half_width_m(width_m: float) -> float:
    """Taxiway clearance half-width (m) from the centerline = wingtip
    reach (½ max wingspan for the width-inferred code letter) + margin.

    Fallback for taxiways with no apt.dat size class (OSM networks);
    prefer :func:`taxiway_clearance_half_width_for_letter`."""
    return taxiway_clearance_half_width_for_letter(
        taxiway_code_letter(width_m))
