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
    "DSF_BUILDINGS",
    "AGP_BUILDINGS",
    "DSF_BUILDING_OSM_OVERLAP_FRAC",
    "DSF_CLUSTER_SIMPLIFY_TOL_M",
    "BUILDING_OUTLINE_FILL_R",
    "BUILDING_OUTLINE_FILL_GATE_M",
    "BUILDING_CLOSE_MIN_PIECE_M2",
    "TERM_BRIDGE_GROUPING",
    "TERMINAL_SIMPLIFY_TOL_M",
    "SLOPING_EDGE_SNAP_M",
    "EMIT_JUNCTIONS",
    "EMIT_APRONS",
    "ENABLE_SERVICE_ROADS",
    "ABSORB_RECTS_ALONGSIDE_APRONS",
    "ENABLE_DISCOVERED_TAXIWAYS",
    "PAINTED_CENTERLINE_FALLBACK",
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
    "CORRIDOR_PROFILE_DAMPING",
    "CORRIDOR_DAMP_ALPHA",
    "FIELD_RUNWAY_ROUTE_BANDS",
    "SERVICE_ROAD_MAX_GRADE",
    "SERVICE_ROAD_WIDTH_M",
    "MIN_SERVICE_STRIP_LEN_M",
    "OSM_SMALL_ROAD_HIGHWAY_TYPES",
    "SERVICE_ROAD_PAVEMENT_NEAR_M",
    "RUNWAY_MAX_GRADE",
    "RUNWAY_END_GRADE",
    "RUNWAY_END_FRACTION",
    "TUNNEL_RAMP_MAX_GRADE",
    "SKIP_TUNNEL_RAMPS_NEAR_ROADS",
    "TUNNEL_ADJACENT_ROAD_DIST_M",
    "TUNNEL_FORK_THROAT",
    "GROUNDSIDE_MAX_GRADE",
    "RUNWAY_VERTICAL_CURVE_K_M",
    "RUNWAY_MAX_GRADE_CHANGE_PER_M",
    "RUNWAY_DEM_FOLLOW_BAND_M",
    "GRADE_VISIBILITY_BUFFER_M",
    "ELEV_ROUNDING_NOISE_M",
    "ROUTE_FIELD_MODEL",
    "ROUTE_FIELD_LOCAL_WINDOW_M",
    "ROUTE_NOISE_FRAC",
    "SURFACE_FAIRING",
    "SURFACE_FAIRING_MAX_MOVE_M",
    "APRON_CORRIDOR_SMOOTH_RADIUS_M",
    "APRON_CORRIDOR_SMOOTH_GRADE",
    "APRON_CORRIDOR_GEODESIC",
    "APRON_CORRIDOR_SEED_RADIUS_M",
    "APRON_BACK_EDGE_GRADE",
    "APRON_BACK_EDGE_RAMPS",
    "TAXI_SLACK_TERMINALS",
    "WRITE_ARBITRATION",
    "TERMINAL_LEAF_LEVELS",
    "TERMINAL_NATURAL_LEVELS",
    "HANGAR_PADS",
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

# Pull TERMINAL and HANGAR building footprints from the DSF
# (user 2026-06-12).  X-Plane places airport buildings as draped
# FACADE polygons (``.fac``) in the Global Airports / scenery-pack
# DSF; ``dsf_reader.read_dsf_buildings`` extracts the footprints of
# the terminal (``term_building_*.fac``) and hangar (``*hangar*.fac``)
# facades.  These are UNIONED with the OSM-derived building outlines
# in ``terminals``/``pipeline`` — DSF is PREFERRED (it is where the
# sim physically renders the building, so grading should match it) and
# OSM fills the gaps where the DSF has no facade.  Off = byte-identical
# to the OSM-only behaviour.  Env override ``O4_DSF_BUILDINGS`` is read
# below, next to HANGAR_PADS (where ``import os as _os`` is in scope).

# When merging the two building sources, an OSM building outline is
# treated as ALREADY covered by the DSF (and dropped in favour of the
# DSF footprint) when this fraction of its area overlaps any DSF
# building footprint.  Below the threshold the OSM building is a
# distinct structure the DSF didn't place and is kept (OSM fills the
# gap).  Lowering it makes the DSF more dominant; raising it keeps more
# OSM buildings.
DSF_BUILDING_OSM_OVERLAP_FRAC = 0.2

# DSF facade-cluster cleanup (user 2026-06-15).  Clustering unions the DSF
# facade pieces with a 0.25 m snap-buffer; that buffer ROUNDS every corner,
# so a complex terminal comes out with hundreds-to-thousands of arc
# vertices (HECA's main terminal: 1,280 verts + 2 spurious interior holes;
# a gate-finger pier: 1,669) — noise that wrecks the downstream outline
# close (it splits on the jagged spine) and the overlap-clip.  Each cluster
# is reduced to a SOLID footprint (buffer-artifact holes filled — a grading
# pad is solid) and DP-simplified at this tolerance to strip the arc noise
# while keeping the real corners.  0.5 m → HECA terminal 1,280→139 verts.
DSF_CLUSTER_SIMPLIFY_TOL_M = 0.5

# Building-pad outline NARROW-GAP FILL (user 2026-06-15).  Gate stands are
# small fingers extending perpendicular off a pier; the gaps between them
# give a terminal a noisy sawtooth boundary that the apron then has to
# step around.  We fill only those NARROW gaps and leave genuine open
# spaces (a U courtyard, the space between two piers, the open centre of a
# finger comb) untouched:
#     closed = pad.close(R)      # dilate→erode: bridges EVERY gap up to 2R
#     fill   = closed − pad      # all the area the close added
#     wide   = fill.open(GATE)   # the WIDE fills — open courtyards / centres
#     result = closed − wide     # keep only the narrow teeth-gaps filled
# R (FILL_R) sets how far the fill reaches to bridge a teeth gap; a gap
# WIDER than 2×GATE (FILL_GATE_M) is reopened as a genuine open space.
# Subtracting the wide fill from the connected closed shape keeps the pad
# in ONE piece (no floating rinds, no severed spines) — which is why this
# replaces the old plain morphological close that left HECA's sparse,
# wide-gapped stands as a sawtooth.  Applied per-pad, so it never merges
# two separate buildings.  MITRE join → straight square edges.  Robust
# across topologies (U-terminals, blob+pier, bars, long buildings) without
# any limb decomposition.  FILL_R = 0 disables (raw pad kept).
BUILDING_OUTLINE_FILL_R = 110.0
BUILDING_OUTLINE_FILL_GATE_M = 55.0

# If the wide-fill subtraction ever pinches the pad into separate blobs,
# each significant piece ≥ this area is emitted as its own pad (the normal
# result is a single connected piece).
BUILDING_CLOSE_MIN_PIECE_M2 = 2000.0

# Douglas-Peucker tolerance for the building-pad simplification pass
# (pipeline, applied to every OSM/DSF terminal+hangar footprint).  A
# small tolerance removes only sub-pad noise — closely-spaced OSM
# vertices and the arc facets left by the DSF facade-cluster snap-buffer
# — that would otherwise spawn sliver triangles in the ear-clip, while
# PRESERVING the real building corners.  Was 2.0 m (user 2026-06-14:
# "dial that back a bit" — at 2 m the more articulated terminal pads
# lost genuine corners, e.g. SPJC terminal4/6 10→7, terminal7/9 11→9).
# 1.0 m recovers those corners; dropping to 0.5 m recovered a few more
# but over-constrained the apron solve (4 new within-shape apron grade
# violations at SPJC) — 1.0 m is the balance point.  The south-concourse
# DSF slabs (true 4-corner rects) stay ~5 verts regardless.
TERMINAL_SIMPLIFY_TOL_M = 1.0

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

# ── Extent-based runway shoulder widening (user 2026-06-12, KPHL) ──
# Shoulders carried by a DSF base-texture layer (e.g. KPHL StarSim's
# whole-airport Groundtextures asphalt.pol ring, 3.7 M m²/87 holes)
# have NO discrete row-110 strip polygon for the whole-polygon
# absorber and NO row-100 declared width for the spec pass — the
# strip along the runway edges falls into residue and emits as apron
# pieces hugging the runway.  This pass measures the pavement itself:
# walk perpendicular from each runway edge through the final source
# union per station; a consistent (high-coverage) strip of
# shoulder-range width on a side is a shoulder → widen the rect over
# it BEFORE the runway subtraction, so the strip becomes runway.
# Scoped to the DSF gap: a side only fires when its strip is mostly
# NOT covered by apt.dat row-110 pavement — row-110-carried shoulders
# stay with the established passes (HECA whole-polygon absorption;
# SPJC's envelope shoulders deliberately live in the junction cut,
# see the INTERSECTION_PROX_M budget in pipeline.py).
# Gate is defined with the other env-overridable flags below (after
# the ``import os as _os``): ``RUNWAY_SHOULDER_EXTENT``.
# Station spacing along the centerline for the perpendicular walk.
RUNWAY_SHOULDER_EXTENT_STATION_M = 25.0
# Outward walk resolution.
RUNWAY_SHOULDER_EXTENT_STEP_M = 1.0
# Shoulder width admitted per side.  Upper bound per FAA AC
# 150/5300-13B / EASA CS-ADR-DSN.B.080: runway + shoulders ≤ 75 m at
# code letter F (60 m runway → 7.5 m/side); 15 m/side is a generous
# envelope over every code.  Anything wider adjoining the runway is
# taxiway/apron slab, never absorbed.  Lower bound filters the ~2 m
# pavement-union simplify tolerance.
RUNWAY_SHOULDER_EXTENT_MIN_M = 2.0
RUNWAY_SHOULDER_EXTENT_MAX_M = 15.0
# Fraction of stations on a side that must show pavement immediately
# past the runway edge ("consistent along the runway").
RUNWAY_SHOULDER_EXTENT_MIN_COVERAGE = 0.8
# DSF-attribution gate: fraction of the strip's sample points allowed
# on the apt.dat-only union before the side is considered row-110-
# carried (established passes own it) and skipped.
RUNWAY_SHOULDER_EXTENT_MAX_APT_FRAC = 0.5


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
# (2026-06-13) APRON BACK-EDGE RAMPS — docs/apron_back_edge_ramps.md.  The
# back strip of an apron (building frontage + gaps BETWEEN buildings, farthest
# from taxi routes) may grade up to this steeper cap so building pads can stay
# flat on sloping terrain: the apron TWISTS — tight 1% taxi-facing front, a
# steeper back that tracks each pad's flat level, ramps between buildings.  4%
# matches groundside / tunnel ramps (drivable, not smooth-for-taxiing).  Only
# back EDGES (both endpoints in the back band) carry it; front-to-back chords
# keep APRON_MAX_GRADE so the transition stays gradual.
APRON_BACK_EDGE_GRADE = 0.040
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
# squeezed-pad exceptions.  The s73-p2 evaluation state had this True so the
# route-justified runway profiles' chain tension could drain into terminals;
# the in-sim verdict (user 2026-06-10, s76) is that sloping pads leave
# BUILDINGS FLOATING (HECA terminal1 spanned 100.2-105.3) — pads must stay
# flat and the connective aprons/taxiways carry the grade.  False = the
# rigid-flat default; squeezed pads still slope via the seed marking
# (_sloped_terminal_nodes), e.g. HECA 6/7/10 straddling two runway levels.
TERMINAL_PADS_SLOPE = False
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
# ── Corridor-profile DAMPING (user 2026-06-14) ──────────────────
# The taxi-corridor field SEEDS at the DEM and projects onto the legal
# band, so wherever the DEM is locally legal the profile sits ON the
# terrain — following its noise too closely.  Real airports grade
# taxiways/aprons "as flat as the terrain allows": the DEM is a noisy
# GUIDE, not a target; max grade is RARE (used only where no flatter
# routing satisfies the constraints), and the only HARD anchors are the
# CIFP runway thresholds + tile seams.  This adds a Laplacian (harmonic)
# smoothing term to the corridor Gauss-Seidel: each soft node diffuses
# toward its neighbours' inverse-distance-weighted mean (minimising
# Σ grade² → the smoothest profile), clamped to its legal band, with the
# 1.5 % caps + hard anchors still binding.  ``CORRIDOR_DAMP_ALPHA`` =
# per-sweep relaxation toward that mean (1.0 = full harmonic; lower =
# gentler / more DEM-near).  Gate ``CORRIDOR_PROFILE_DAMPING``
# (``O4_CORRIDOR_DAMP``) — OFF restores the pure DEM-follow.
CORRIDOR_DAMP_ALPHA = 0.5
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
# Skip tunnel-portal ramp emission where the tunnel runs under / alongside
# OTHER roads (user 2026-06-12, LMML): in a dense road interchange the
# surface walk traces a tangle of parallel carriageways, slip roads and
# roundabouts, and the ramps overlap.  Rather than model that complexity,
# skip ramp emission for any tunnel that has another road CROSSING it or
# running within ``TUNNEL_ADJACENT_ROAD_DIST_M`` of it.  The test excludes
# (a) ``highway=service`` minor roads, (b) other tunnels (a divided
# highway's own clustered carriageway), and (c) shared-node continuations
# (the surface road the ramp is meant to follow) — so an isolated tunnel,
# or one crossed only by service roads / its own carriageway, still emits
# ramps (SPJC's user-approved tunnels are kept; all 6 LMML tunnels skip).
SKIP_TUNNEL_RAMPS_NEAR_ROADS = True
TUNNEL_ADJACENT_ROAD_DIST_M = 15.0
# Y-fork throat junction (user 2026-06-12, KPHL RWY 26 north portal:
# road+rail share a bore then fork outside).  When True, the diverging
# end of a Y-split tunnel is modelled like a taxiway sloping-rect +
# junction: a single ``node_altitudes`` "throat" polygon with a V-notch
# bridges the shared bore to the per-arm sloping rects, and a continuous
# retaining wall traces the whole Y (outer fan edges + the inner V
# between the arms).  No pavement is graded between the arms.  When False
# the legacy Y-split (advance each branch clear of its siblings, leaving
# the crotch bare) is byte-identical — only the FORK path is affected;
# parallel-bore clusters (SPJC divided highways) are untouched either way.
TUNNEL_FORK_THROAT = True
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
#   * A within-shape grade constraint exists between two MUTUALLY-VISIBLE
#     vertices — a pair whose straight chord stays inside the polygon (grown by
#     ``GRADE_VISIBILITY_BUFFER_M``).  Visibility is the gate: a chord that
#     cuts across a non-convex notch is a phantom path and excluded.
#   * Under ``ROUTE_FIELD_MODEL`` (below) visibility chords are additionally a
#     LOCAL law only: pairs longer than ``ROUTE_FIELD_LOCAL_WINDOW_M`` are not
#     graded against each other (ring-adjacent pairs — the physical edge —
#     always are); the LONG-RANGE law is the route-band check instead.
#   * ``ELEV_ROUNDING_NOISE_M`` absorbs single-decimal (0.1 m) altitude rounding.
GRADE_VISIBILITY_BUFFER_M = 1.0
ELEV_ROUNDING_NOISE_M = 0.15

# ── ROUTE-FIELD MODEL (#3, user-approved s73-p3, built s75; see
# docs/route_field_model.md) ─────────────────────────────────────────────
# The long-range within-pavement grade law is the TAXI-ROUTE distance from
# the hard anchors (runway nodes at solved values, seam/threshold pins,
# corridor-held writes): a vertex's feasible band is the intersection over
# anchors a of [E_a ± cap·route_d(a, v)·(1 + ROUTE_NOISE_FRAC)].  Grade
# rules (ICAO Annex 14 §3.9, EASA CS-ADR-DSN.D.265/.280) regulate slope
# along the taxi route; nothing regulates the straight chord between two
# points kilometres apart, and km-scale visibility chords systematically
# UNDER-measure the route (corner cuts, cross-shape chains) — at HECA the
# hard 05C contact reached the taxiway-A apron mouth through ~2.5 km of
# chained chords where the real route is ~3.08 km, an 8.5 m manufactured
# infeasibility (s73-p10g).  Visibility chords survive only as a LOCAL
# smoothness cap (pairs ≤ ROUTE_FIELD_LOCAL_WINDOW_M; ring-adjacent pairs
# always).  The validator (tools/check_grade.py) and the runtime WARN
# change IDENTICALLY with this flag — the definition of a violation
# changes, so the model must never ship solver-only or validator-only.
ROUTE_FIELD_MODEL = True
# Local smoothness window (m): visibility-chord pairs at or under this
# length keep the chord grade law (design start 80, measure 60–100).
ROUTE_FIELD_LOCAL_WINDOW_M = 80.0
# FINAL SURFACE FAIRING (s76, user in-sim feedback): the dense all-pair
# chord web used to act as an implicit smoother — with chords windowed,
# sub-cap DEM noise survives the solve as visible ripples at junctions.
# A final weighted-Laplacian fairing pass irons them: soft uncoupled
# vertices relax toward their grade-graph neighbours, clamped into the
# route bands and a per-node displacement budget (so it smooths ripples
# without re-levelling surfaces), then caps are re-projected.
SURFACE_FAIRING = True
SURFACE_FAIRING_MAX_MOVE_M = 0.5
# APRON CORRIDOR SMOOTHING (s76, user in-sim verdict at CYXY: "the apron is
# much too steep ... use taxi route corridors along the edges or into aprons,
# and ensure apron grade outward from those in maybe a 200 m radius is graded
# at ideally 1 %").  Within this radius of a taxi corridor (apt.dat/OSM
# centerline OR a taxi rect's source axis — discovered taxiways carry no
# apt.dat row), apron vertex pairs are projected toward this grade as a
# best-effort SOLVER PREFERENCE after the enforce.  The LEGAL cap (and the
# validator's law) stays ROLE_GRADE_LIMITS — "ideally" means the projection
# plateaus wherever hard anchors genuinely demand more.  Radius 0 or grade 0
# disables.
APRON_CORRIDOR_SMOOTH_RADIUS_M = 200.0
APRON_CORRIDOR_SMOOTH_GRADE = 0.010
# GEODESIC corridor binding (s77 investigation, user-approved): measure the
# zone by the shortest INTERIOR path through pavement (multi-source Dijkstra
# over the solver's edge graph) instead of straight-line distance — a vertex
# 13 m across grass from a centerline is NOT served by it — and additionally
# clamp in-zone apron vertices into corridor-VALUE bands
# [corridor_alt ± grade·interior_distance] propagated at the smoothing grade,
# so an apron cannot sit on a uniform offset (wall) from the corridor that
# serves it — internal pair-cap scaling alone cannot see that.  Still a
# best-effort preference: bands yield to the legal route-law bands wherever
# they conflict (the squeeze/arbitration families).  False restores the
# straight-line zone test and pair-only smoothing.
APRON_CORRIDOR_GEODESIC = True
# Corridor-adjacent vertices SEED the geodesic field at their own solved
# values: any pavement vertex within this straight-line distance of a
# corridor polyline (≈ on the corridor surface), plus every taxi-rect
# vertex (the rect IS the corridor; wide rects' corners sit beyond any
# small threshold).
APRON_CORRIDOR_SEED_RADIUS_M = 15.0
# The route graph under-counts real taxi routes by ~4 % (straight endpoint
# stubs, uncurved row joins — s73-p3 measured).  Route bands used as HARD
# constraints carry this relative margin; the validator MUST use the same
# margin or it flags the solver's own legal output.
ROUTE_NOISE_FRAC = 0.04
# WRITE-LAYER ARBITRATION (s77, user-approved): when a corridor tie cannot
# reach its consensus value (route-law anchors block and the blocker
# rescue does not apply), the tie used to be DROPPED entirely — the two
# chains then wrote values metres apart at one junction and the
# disagreement stood in the surface as a wall on whatever spans the seam
# (HECA #256: G@100.9 held against T@104.2 free-pinned, 3.3 m over
# 11.5 m, per-axis exempt but a cliff to the eye).  Instead, accept a
# PARTIAL tie: clamp the consensus value into the member chain's
# anchor-feasible interval and anchor there — each chain moves as close
# to agreement as its own route law allows, shrinking the wall to the
# genuine route-law residual.  The runway-flex demand synthesis still
# fires from the ORIGINAL consensus value, so arbitration never masks a
# legitimate flex demand.  False restores drop-on-skip.
WRITE_ARBITRATION = True
# TERMINAL LEAF LEVELS (s77 user ruling, supersedes "terminals must not
# rise"): terminal pads are natural LEAF nodes — rigid-flat, but their
# LEVEL follows the apron(s) they connect to (up or down) through the
# grade projection, instead of being pre-calculated from taxi-route seed
# bands and locked.  Pads re-level to their median for coherence, are
# band-EXEMPT (their own route bands are graph-entry-noisy; the aprons
# they follow are themselves band-clamped), and move as rigid level
# groups in every projection.  A pad sharing a hard node stays held.
# False restores the s76 seed-ceiling + freeze behaviour.
TERMINAL_LEAF_LEVELS = True
# NETWORK PROFILE MODEL (#4, docs/network_profile_model.md — user-approved
# s77p4: "solve the full centerline taxi network, which includes curves,
# solve every intersection, similar to crossing runways, so they always
# agree, then map that to the geometry").  ONE elevation profile is solved
# over the COMPLETE centerline graph (auto_patch/network_profile.py):
# intersections are shared vertices (agreement by construction — the tie /
# consensus / freeze layer is bypassed), runway contacts are hard anchors
# whose infeasibility against the rest of the field emits the runway-flex
# demand DIRECTLY, jointly-infeasible squeezes spread minimax along the
# route instead of standing as walls at seams, and the corridor write
# layer (stations, rect planes, junction twist) SAMPLES the field.  The
# singleton `_touches_runway` chain gate lifts under this model (taxiway-B
# class stubs profile from the field; there is no tie network to spread a
# squeeze — the s77p3 revert reason).  Requires TAXI_CORRIDOR_PROFILE
# (the corridor pass is the carrier).  False restores the s77 tie-layer
# behaviour byte-identically.
NETWORK_PROFILE_MODEL = True


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
    # Building pads (terminals / hangars / towers): drives the
    # flat-vs-graded code path — see TERMINAL_MAX_GRADE.  The role was
    # renamed from "terminal" (user 2026-06-12); the legacy key stays
    # as a read alias so check_grade still validates pre-rename
    # patches on disk.
    "building":           TERMINAL_MAX_GRADE,
    "terminal":           TERMINAL_MAX_GRADE,  # legacy alias (read-only)
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

# When apt.dat has NO 1201/1202 taxi-route network, synthesize the taxi
# centerline set from its row-120 PAINTED lines (paint codes 1/7/51/57 =
# the solid-yellow centerline family) after basic is-it-really-a-
# centerline checks (on-pavement, not boundary-hugging like an edge
# line, runway footprint clipped) — see
# ``apt_dat_reader.painted_taxi_centerlines``.  Small Global Airports
# fields routinely ship only painted lines; without this they build no
# taxi rects, their aprons read runway-disconnected, and the discovered-
# strip fallback reconstructs a much cruder network (user 2026-06-11;
# KOQN).  Airports WITH a 1201/1202 network are untouched (cross-
# referencing painted curves against the network is future work).
PAINTED_CENTERLINE_FALLBACK = True

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

# (s79) TERMINAL PERPENDICULAR-CHORD LAW — ★ USER RULING 2026-06-12:
# terminals adjust UP OR DOWN so that a perpendicular chord from each
# taxi centerline that intersects the terminal does not exceed this
# grade.  The perpendicular construction naturally selects LATERAL
# serving taxiways (a head-on gate lane's perpendiculars miss the pad),
# which kills the bowl-self-certification that defeated the previous
# adjacent-apron-median and corridor-1%-plane bounds (HECA terminal1
# at 100.1 vs stub B 102.3 only 36 m away).  Under the apron-follows
# model (TERMINAL_NATURAL_LEVELS) the rule holds BY CONSTRUCTION —
# the apron at the pad face sits on the corridor plane and the pad
# inherits it — so it is checked as a VALIDATOR warn, not solved for
# (the s79 solver-side lift was measured-rejected: the pad landed
# right but the apron behind it kept the bowl as within-pairs).
TERMINAL_CHORD_MAX_GRADE = 0.01
# Max perpendicular chord length.  400 m (user 2026-06-12): the
# terminal level must be adjusted so the apron grades at ~1 % to its
# serving taxiways — at 200 m the reach missed HECA's taxiway S
# 350-400 m from the big pad, so its 1 % demand never entered the
# window and the apron between settled at 1.3-1.4 %.  Where two
# taxiways' 1 % demands conflict (window inverts), the construction
# falls back to the APRON_MAX_GRADE law-rate window with the 1 %
# least-violation midpoint — the preference yields to the law, never
# the reverse.
TERMINAL_CHORD_REACH_M = 400.0

# (s80) APRON-FOLLOWS RE-SOLVE — docs/apron_follows_resolve.md (user
# direction 2026-06-12: terminals = a NATURAL RESULT of grading the
# apron correctly).  One-way dependency, no back-edges:
#   network field → taxi rects/junctions → APRONS → TERMINAL PADS.
# Under the gate: (a) pads are TRANSPARENT in the solve — ordinary
# graded nodes (TERMINAL_MAX_GRADE cap), no taxi-route seed ceiling,
# no rigid flat-coupling, no holds through the apron projections (this
# is NOT the twice-rejected rigid-free pad: there is no rigidity to
# drag; flatness is imposed AFTER from the median); (b) inside the
# corridor geodesic zone the apron's attractor is the CORRIDOR-PLANE
# value instead of the DEM (the bowl's second parent); (c) each pad
# INHERITS the median of its own settled nodes, then flattens —
# measured acceptance: a flatten that adds within-violations to its
# apron complex reverts to the settled (sloped) surface, so the pad
# can never out-run its own apron; (d) the outer-rim terrain-break
# retreat may fire beyond the corridor zone even when the apron
# interior is intentionally above the DEM.  OFF = the s79 behaviour
# byte-identically.
TERMINAL_NATURAL_LEVELS = _os.environ.get("O4_TERMINAL_NATURAL", "1") == "1"

# (2026-06-13) APRON BACK-EDGE RAMPS — docs/apron_back_edge_ramps.md (user
# direction: "allow just the back edge of aprons — the ones farthest from taxi
# routes — to go up to grade, so the buildings can be flatter and the apron
# twists slightly to meet them with ramps between, but the majority of the
# apron stays at 1%").  Extends TERMINAL_NATURAL_LEVELS: the apron strip behind
# / between the building pads is allowed to grade at APRON_BACK_EDGE_GRADE (4%)
# instead of the 1.5% apron law, so the pairwise pad resolution no longer drags
# adjacent pads to a compromise level and the FLAT-vs-SLOPE acceptance no longer
# reverts a flatten over a legal back ramp.  The front / interior is never
# relaxed (corridor smoothing still holds it at 1%).  Default ON (user
# 2026-06-13, for in-sim eval); O4_APRON_BACK_RAMPS=0 disables → byte-identical
# to the TERMINAL_NATURAL_LEVELS behaviour (the whole feature is gated).
APRON_BACK_EDGE_RAMPS = _os.environ.get("O4_APRON_BACK_RAMPS", "1") == "1"

# TAXI-NETWORK SLACK for flat terminals (user ruling 2026-06-16, docs/
# taxi_slack_terminals.md).  Replaces the back-edge-ramp philosophy: instead of
# letting the APRON grade at 4% to keep a building flat, the serving taxi
# CORRIDORS flex STEEPER within their runway-anchored route bands so the apron
# stays at 1% (1.5% only when 1% is infeasible even after flexing).  A building
# straddling terrain — whose serving corridors sit at very different elevations
# — stays flat at a level the band-widened chord window allows, raised out of
# any DEM canyon; it slopes only when even the 1.5% band window inverts.
# Default ON (user 2026-06-16, for in-sim eval).  O4_TAXI_SLACK=0 disables →
# byte-identical to the pre-feature behaviour.
TAXI_SLACK_TERMINALS = _os.environ.get("O4_TAXI_SLACK", "1") == "1"

# (apron-edge-retreat REMOVED 2026-06-16, user ruling): a post-solve pass
# (`_retreat_route_pinned_apron_edges`) used to move apron polygons inward
# to break a weld and render a cliff against a high neighbour (HECA #198
# road).  It MUTATED GEOMETRY during the elevation solve and false-fired at
# plain taxiway-rect junctions under a sharp DEM (apt_smoothing_pix=4),
# opening the HECA stub-B↔apron gap.  Deleted outright: the road ramp grades
# fine without it, and nothing should reshape pavement post-solve.

# (s81) HANGAR PADS — docs/hangar_pads.md (user rulings 2026-06-12).
# When ON, ``aeroway=hangar`` buildings are ALWAYS admitted into the
# building-pad list alongside terminals and treated identically (weld,
# apron-follows inherit, groundside).  Previously hangars only entered
# via the no-terminal fallback (user 2026-04-28, HECA mistagging);
# ``aeroway=tower`` keeps that fallback-only behaviour.  Taxi
# centerlines that enter a building footprint stop at the building
# edge and weld to it (rects never contest pad area — the failure
# mode that motivated the old guard).  OFF = fallback-only admission,
# byte-identical to pre-s81.
HANGAR_PADS = _os.environ.get("O4_HANGAR_PADS", "1") == "1"
# Corridor-profile Laplacian damping (see CORRIDOR_DAMP_ALPHA above).
# Default ON (user 2026-06-14): with FIELD_RUNWAY_ROUTE_BANDS the bands
# carry real slack, so the harmonic smoothing now halves corridor
# grade-change (HECA kinks >1%: 56→23) and settles aprons toward terrain
# instead of being a no-op.  O4_CORRIDOR_DAMP=0 restores the pure DEM-follow.
CORRIDOR_PROFILE_DAMPING = _os.environ.get("O4_CORRIDOR_DAMP", "1") == "1"
# Junction node-altitude RIPPLE smoothing (user 2026-06-15): the twist
# pass leaves a free junction RING vertex bowed off the line between its
# two ring-neighbours — a grade-CHANGE (curvature) kink under the 1.5 %
# cap, so the grade-magnitude smoother never touches it (user: "the shape
# edges are welded and matched correctly but there's a little ripple
# before getting into the heart of the junction; that second node needs
# to be averaged between the shape edge node and the third one in").  A
# ring-Laplacian pass averages each FREE (un-welded, non-rect-corner)
# vertex toward the distance-linear interpolation of its ring neighbours,
# HOLDING welded/shared and sloping-rect-corner vertices (so no
# cross-shape step).  O4_JCT_RIPPLE=0 disables it.
JUNCTION_RIPPLE_SMOOTH = _os.environ.get("O4_JCT_RIPPLE", "1") == "1"
# Field RUNWAY-anchor route bands (user 2026-06-14): measure the
# network-profile field's runway-anchor feasibility band along the
# centerline TAXI ROUTE (taxi_routing) instead of the field graph.  The
# field graph carries chord + proximity coupling edges that shortcut
# STRAIGHT across apron/junction interiors, so a runway contact reachable
# in 146 m of pavement-geodesic is really ~350 m along the taxiway an
# aircraft (and the graded surface) follows — the field floors the apron
# ~1-3 m too high, lifting it off the terrain (the bump the user reports).
# Mirrors the enforce's _runway_reach_bands (already route-measured); the
# field was the one out of step.  Seam/threshold pins keep the field-graph
# entry.  Default ON (user 2026-06-14); O4_FIELD_RW_ROUTE=0 restores the
# pure field-graph band (byte-identical).
FIELD_RUNWAY_ROUTE_BANDS = _os.environ.get("O4_FIELD_RW_ROUTE", "1") == "1"

# DSF terminal/hangar building footprints (user 2026-06-12) — see the
# documented block near LOAD_DSF_PAVEMENT above.  Read here because
# ``import os as _os`` only comes into scope at this point in the file.
DSF_BUILDINGS = _os.environ.get("O4_DSF_BUILDINGS", "1") == "1"

# (20260617) AGP HANGAR BUILDINGS (user 2026-06-17): X-Plane also places
# airport hangars as ``.agp`` AUTOGEN POINTS — a single ``OBJECT`` handle
# + heading in the DSF, with the footprint encoded in the ``.agp`` sidecar
# (TILE/CROP_POLY in texture pixels × TEXTURE_WIDTH/HEIGHT ÷ TEXTURE_SCALE,
# anchored at ANCHOR_PT).  ``dsf_reader.read_dsf_buildings`` resolves the
# sidecar through the X-Plane ``library.txt`` map and projects the footprint
# onto the handle, feeding it into the SAME building pool as the ``.fac``
# facades (role ``"hangar"``).  Scoped initially to the
# ``lib/airport/Common_Elements/Hangars/`` virtual prefix.  Default ON
# (user 2026-06-17, for in-sim testing); O4_AGP_BUILDINGS=0 disables it
# (byte-identical to the prior .fac-only behaviour).  Has no effect
# unless DSF_BUILDINGS is also ON (shares the building path).
AGP_BUILDINGS = _os.environ.get("O4_AGP_BUILDINGS", "1") == "1"

# (20260614-02) TERM-BRIDGE GROUPING (user 2026-06-14): X-Plane's
# Terminal_kit ships ``term_bridge_*.fac`` connector facades (enclosed
# skybridges / link spans) that physically join two ``term_building_*``
# facades.  When ON, these bridge footprints are fed into the DSF
# building clustering as CONNECTORS so a building + bridge + building
# run unions into ONE pad and grades as a single flat group (the
# bridged concourses sit at a common level).  OFF = bridges ignored
# (the prior behaviour, byte-identical to DSF_BUILDINGS alone).  Has
# no effect unless DSF_BUILDINGS is also ON.
TERM_BRIDGE_GROUPING = _os.environ.get("O4_TERM_BRIDGE_GROUPING", "1") == "1"

# (s79) INTERIOR-PATH ENTRIES — docs/interior_path_entries.md.
# ★ USER RULING 2026-06-11: no shape may ever check grade ACROSS GRASS.
# Every off-graph entry into the centerline route graph (route-band law
# anchors/check vertices, the network-profile field's law-entry gap
# edges and band anchors, _runway_reach_bands gap charging) charges the
# IN-PAVEMENT path length instead of the straight chord; no interior
# path ⇒ no coupling.  Solver, field and validator share ONE measure
# (auto_patch/interior_path.py) — partial application is the measured
# failure mode (s78p5 revert; s79 field-only experiment = 50 viol).
# OFF restores the straight-gap behaviour byte-identically.
INTERIOR_PATH_ENTRIES = _os.environ.get("O4_INTERIOR_PATH", "1") == "1"

# (s80) Extent-based runway shoulder widening — tuning constants and
# rationale with the other RUNWAY_SHOULDER_EXTENT_* values near the
# DSF block above.  ``O4_SHOULDER_EXTENT=0`` restores the pre-s80
# build (shoulder strips carried only by DSF pavement fall into
# apron residue along the runway).
RUNWAY_SHOULDER_EXTENT = _os.environ.get("O4_SHOULDER_EXTENT", "1") == "1"

# (2026-06-17) RUNWAY-SHOULDER SEGMENTATION REACH — docs/runway_
# shoulder_detection.md.  The runway-segmentation breakpoint collector
# splits the runway where adjacent pavement / taxiway polygon edges
# CONTACT it, but its proximity budget is a FIXED generic ~7.6 m FAA
# shoulder allowance.  When apt.dat row-100 declares an EXPLICIT
# shoulder width (``shoulder_code // 100`` ≥ 1, e.g. OMAA's 20 m), the
# real paved edge a taxiway connects to sits at runway-half + that
# shoulder (50 m from a 60 m runway's centerline), well past the 42 m
# the fixed budget reaches — so the exit's contact never becomes a
# seam and the runway segment boundary lands at the wrong longitudinal
# position (the OMAA 13R/31L gap).  ON ⇒ the contact budget for a
# shouldered runway is its apt.dat-coded shoulder + chart tolerance, so
# seams land where pavement meets the shoulder edge as defined in
# apt.dat.  Runways with NO coded shoulder (code < 100) keep the 7.6 m
# budget ⇒ byte-identical.  Env override ``O4_SHOULDER_SEGMENT``.
RUNWAY_SHOULDER_SEGMENT = (
    _os.environ.get("O4_SHOULDER_SEGMENT", "1") == "1")

# (20260616) JUNCTION CENTERLINE SPINE — docs/junction_centerline_spine.md.
# Junctions/aprons emit as a single ring polygon, so X-Plane interpolates
# the interior between boundary-only node_altitudes and a taxi centerline
# crossing the INTERIOR (no vertices on it) waves instead of tracking the
# solver's clean ≤1.5% corridor profile (OMAA taxiway H @ junction -10225:
# field flat 1.5% but the emitted surface spikes to 3.6%).  When ON, each
# junction/apron is SLICED along every crossing taxi centerline (pre-solve
# pure geometry) so the centerline becomes a real shared edge the solver
# grades — see junction_spine.py.  Default ON (2026-06-17, user — enabled
# in dev for in-sim testing); set O4_JCT_SPINE=0 to disable / restore the
# byte-identical ring junctions.  Outstanding issues: STATUS.md 20260617-01.
JUNCTION_CENTERLINE_SPINE = _os.environ.get("O4_JCT_SPINE", "1") == "1"
# Spacing (m) of spine nodes densified along each crossing centerline
# inside a junction.
SPINE_STEP_M = float(_os.environ.get("O4_JCT_SPINE_STEP_M", "12.0"))

# (20260618) RECT END-CAPS — STATUS.md 20260618-01.  A centerline-spine
# slice ending at a SLOPING taxi rect used to weld a mid-edge node onto the
# rect's long edge, flipping the clean 4-corner sloping plane to
# ``node_altitudes`` so it graded only ~half its length (SPJC taxiway L
# dropped 3.8 m of a 7.5 m drop), starving the apron of slack.  When ON,
# each sloping rect is carved 2 m at every JUNCTION-FACING flat end at
# RECT-BUILD TIME (Phase 1, before junctions form as ``pav_union − rects``
# and before any elevation); the carved strip is emitted as a junction cap
# and subtracted from the residue.  The rect body stays a full-length
# 4-corner plane (the spine now welds onto the FLAT cap's edge, a soft
# junction edge), and the solver grades the cap like any other junction so
# the rect end settles to the junction level on its own.  Default OFF =
# byte-identical (no caps carved).  Two earlier Phase-2 attempts regressed
# within-shape grade — the PHASE, not the role, was the bug.
# (20260618 W2) CLEAN ENFORCE BANDS — docs/grade_enforcement_plan.md.
# The ROUTE_FIELD enforce band self-anchors on the CURRENT solved field
# (NETWORK_PROFILE_MODEL `extra_points` + the field's route-graph view), which
# manufactures band INVERSIONS (lo>hi) on nodes the feasibility oracle proves
# compliant → those nodes get HELD out of the projection → feasible grade
# violations can never be fixed.  When ON, the hard band drops the field
# self-anchor (keeps the legitimate runway/seam/held-write/terminal anchors,
# so building-flatten's terminal anchoring survives) and the field-tie moves
# to the projection's movement-minimising seed + the corridor attractor.
# Default OFF = byte-identical.
W2_CLEAN_BANDS = _os.environ.get("O4_W2_BANDS", "0") == "1"

RECT_END_CAPS = _os.environ.get("O4_RECT_CAPS", "0") == "1"
# Depth (m, perpendicular to the rect's flat end) of each end-cap — strictly
# beyond verification.check_vertex_on_flat_edge's EDGE_PROX_M (1.5 m) so no
# junction vertex lands in the rect's exclusion band.
RECT_END_CAP_DEPTH_M = float(_os.environ.get("O4_RECT_CAP_DEPTH_M", "2.0"))

# (s79) ON-PAVEMENT service-road carve — docs/service_road_carve.md.
# ★ USER RULINGS 2026-06-11: roads = apt.dat 1206 routes ONLY (no
# polygon/OSM detection); only pavement narrower than the cross-section
# cap is classified; nothing near a terminal; roads WORK LIKE TAXIWAYS
# — qualifying runs join the centerline set as ``SVC*`` refs and ride
# the single rect → junction → absorption decomposition with role
# ``service_road`` (4 %).  Independent of ``ENABLE_SERVICE_ROADS`` (the
# deferred OSM small-road / off-pavement builder).  DEFAULT ON for the
# user's in-sim evaluation (2026-06-12; Steps C/D landed @b391e27 —
# CYXY roads-on 0/0/0, HECA 57/0/0 invariants held);
# ``O4_SERVICE_ROAD_CARVE=0`` restores the road-less build.
SERVICE_ROAD_CARVE = _os.environ.get("O4_SERVICE_ROAD_CARVE", "1") == "1"
# Max perpendicular pavement cross-section for ROAD classification.
# User rule "< 10 m"; measured at the HECA #198 switchback legs:
# 8.2-9.4 m and 12.2 m (the fused DSF pavement includes shoulder) →
# 13 m so both legs qualify (pending the user's KML verdict).
ROAD_CARVE_MAX_WIDTH_M = 13.0
# Terminal guard (refined, user 2026-06-11 round 3): drop a road sample
# near a terminal only when the route runs ALONGSIDE it (locally
# parallel within the angle below) — a road passing a terminal CORNER
# perpendicular/diagonally is a real road (HECA terminal4 → junction
# #168 section).  Terminal curbside pavement is already subtracted from
# pav_union by the groundside pass, so this is a second line.
ROAD_CARVE_TERMINAL_CLEAR_M = 30.0
ROAD_CARVE_TERMINAL_PARA_DEG = 35.0
ROAD_CARVE_SAMPLE_M = 6.0           # sampling step along 1206 routes
ROAD_CARVE_MIN_RUN_M = 20.0         # min qualifying run to become road
# Mode C (edge-hugging): a sample within this of the pavement BOUNDARY
# qualifies even when the cross-section is blended-wide — a road along
# the airside rim is "not surrounded by apron" (user round 3; HECA
# terminal-corner section gaps 4.2-8.4 m, CYXY pav[1] 1-7.4 m).
ROAD_CARVE_EDGE_HUG_MAX_M = 8.5
# (s80) ROAD-FRONTAGE GRADE LAW — a within-shape pair (apron/junction)
# whose BOTH endpoints sit within this of a service-road polygon is
# governed by the ROAD's 4 % law, not the shape's 1.5 %: the carve
# welds its corners into the host ring, so the strip alongside the
# road is physically part of the road's descent (CYXY road #30: the
# apron-ring frontage edge read the road's 2.5 % drop as a 3.13 %
# apron violation while every surface obeyed its own law; the squeeze
# is hard-anchored — runway contact 12 m below — so no legal apron
# value exists).  Mirrors the per-axis junction model: the road law
# rides ALONG the carve; pairs reaching away from it stay strict.
# VALIDATOR-ONLY (tools/check_grade._check_within_shape): the solver
# keeps fighting at the strict cap (status quo) — relaxing its edge
# caps too let road-welded rims sag with the road and broke 1.5 %
# pairs against strict nodes just OUTSIDE the zone (HECA apron #258
# grew a 3 m pit, pairs 5-8 %; measured s80) — the in-zone/out-zone
# transition needs a taper before the solver may use this law.
ROAD_FRONTAGE_TOL_M = 3.0
# … but NOT the rim roads that run ALONG the terminal row (user round
# 4: "they would just get absorbed by the apron anyway") — an edge-hug
# sample within this radius of a terminal whose route runs parallel
# (≤ ROAD_CARVE_TERMINAL_PARA_DEG) to the nearest terminal edge is
# dropped.  Perpendicular corner-passers (the HECA terminal4 →
# junction #168 section, 70°) keep.  Modes A/B are unaffected.
ROAD_CARVE_TERMINAL_RIM_M = 300.0

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
