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
    "AXIS_ALIGN_TOL_DEG",
    "LOAD_DSF_PAVEMENT",
    "SLOPING_EDGE_SNAP_M",
    "EMIT_JUNCTIONS",
    "EMIT_APRONS",
    "EMIT_BRIDGES_AND_TUNNELS",
    "JUNCTION_CLUSTER_DIST_M",
    "MAX_BOUNDARY_EDGE_M",
    "MIN_SEGMENT_LEN_M",
    "NECK_ABSOLUTE_M",
    "NECK_ABSORB_FRAC",
    "NECK_RELATIVE",
    "ROLE_GRADE_LIMITS",
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

# Combine apt.dat with DSF pavement polygons: when True the
# smart-apt.dat selector still runs to choose the best custom-pack
# vs global candidate by OSM coverage; DSF polygons supplement
# whichever apt.dat is picked.
LOAD_DSF_PAVEMENT = True


# Per-role within-shape grade limits (rise / run, decimal — i.e.
# 0.015 = 1.5%).  The validator in tools/check_grade.py uses this
# table to decide whether a vertex pair on a polygon's ring is in
# violation.  ``None`` means "skip the within-shape grade check
# for this role" — used for shapes that intentionally trace
# terrain (boundary outline, groundside curbside) or that are
# vertical structures (retaining walls).
#
# Keep this aligned with the solver caps in ``elevation.py``
# (``TAXI_MAX_GRADE``, ``APRON_MAX_GRADE``).  Per user 2026-05-07
# apron and junction get the same 1.5% all-directions cap as
# taxiways; per user 2026-05-08 tunnel ramps get 4.0%.
ROLE_GRADE_LIMITS = {
    # Taxiway-like surfaces — 1.5% along centerline (axis), tested
    # here as 1.5% between any pair of ring vertices since the
    # ring follows the axis closely.
    "runway":             0.015,
    "primary_parallel":   0.015,
    "secondary_parallel": 0.015,
    "stub":               0.015,
    "cross_connector":    0.015,
    # Apron / junction — 1.5% all directions within the polygon
    # (per user 2026-05-07).
    "apron":              0.015,
    "junction":           0.015,
    # Terminals are typically flat polygons; the value rarely fires.
    "terminal":           0.015,
    # Tunnel ramps descend from pavement elevation to the tunnel
    # floor; 4% is the navigable taxi grade for ramped portals
    # (per user 2026-05-08).
    "tunnel_ramp":        0.040,
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
    "groundside_pavement": 0.040,
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
PATCH_SLOPE_PROFILE = "spline"   # "spline" | "plane" (only matters if cut)


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
