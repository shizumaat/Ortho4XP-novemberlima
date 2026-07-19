"""PavementLayout + BuiltShape data model and serialisation.

Holds the pavement-builder's core data types (the dataclasses every
phase reads + writes), the role-tag vocabulary, the meter-anchored
projection helpers used to construct a layout, and the .osm
serialisation method (``PavementLayout.to_osm``).

Phase-1 (geometry) and Phase-2 (elevation) both populate
``BuiltShape`` instances inside a ``PavementLayout``; the .osm
emission turns the meter-space layout back into JOSM-readable
WGS-84 OSM with shared node IDs.

Public API:
    BuiltShape, PavementLayout              — data classes
    ROLE_*                                  — role-tag constants
    AEROWAY_FOR_ROLE                        — role -> aeroway tag value
    SHARED_VERTEX_TOL_M, R_EARTH            — geometry constants
    airport_anchor(apt), projection(anchor) — meter-space helpers

Used by every O4_Pavement_* module.  Sits at the bottom of the
pavement-builder dependency hierarchy alongside Pavement_Config.
"""
from __future__ import annotations

import math
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, overload

import O4_UI_Utils as UI

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry

from . import apt_dat_reader as APR
from .geom_safe import min_rotated_rect
from .pavement import strips as PS

from .config import (
    SLIVER_ANGLE_THRESHOLD_DEG,
    TAXI_GRADE_BY_WIDTH,
    TAXI_GRADE_WIDTH_ROLES,
    taxiway_code_letter,
)

if TYPE_CHECKING:
    from .canonical_points import CanonicalPointRegistry

# Narrow exception tuple for shapely / numeric-geometry failure
# modes.  Programming errors propagate so they surface immediately.
_GEOM_EXC = (ValueError, TypeError,
             GEOSException, TopologicalError, IndexError)

__all__ = [
    "BuiltShape",
    "PavementLayout",
    "R_EARTH",
    "SHARED_VERTEX_TOL_M",
    "vertex_bucket",
    "corner_alts_from_high_low",
    "high_low_from_corner_alts",
    "ROLE_RUNWAY",
    "ROLE_PRIMARY_PARALLEL",
    "ROLE_SECONDARY_PARALLEL",
    "ROLE_STUB",
    "ROLE_CROSS_CONNECTOR",
    "ROLE_APRON",
    "ROLE_BUILDING",
    "ROLE_JUNCTION",
    "ROLE_RUNWAY_CROSSING",
    "ROLE_BOUNDARY",
    "ROLE_TUNNEL_RAMP",
    "ROLE_RETAINING_WALL",
    "ROLE_GROUNDSIDE_PAVEMENT",
    "ROLE_SERVICE_ROAD",
    "ROLE_SERVICE_JUNCTION",
    "ROLE_BRIDGE_TRENCH",
    "ROLE_BRIDGE_CAUSEWAY",
    "ROLE_TUNNEL_TRENCH",
    "AEROWAY_FOR_ROLE",
    "_airport_anchor",
    "_projection",
]


# ──────────────────────────────────────────────────────────────────
# Geometry constants
# ──────────────────────────────────────────────────────────────────
from O4_Geo_Utils import earth_radius as R_EARTH  # single source of truth
SHARED_VERTEX_TOL_M = 0.5    # snap vertices closer than this together

# NO-STACKED-NODES INVARIANT (owner ruling 2026-07-19, completing the
# user 2026-05-18 invariant "two nodes can never share the same
# location without sharing the same elevation"): coincident vertices
# ALWAYS intern to ONE node with ONE consensus elevation — the emitter
# never mints a second node id at the same canonical coordinate.  A
# genuine level change must be HORIZONTAL wall geometry (two node
# columns offset in plan — the ``retaining_wall`` machinery), never
# coincident nodes with different elevations: those render as bare
# near-vertical mesh tears (the CYXY d=0.00 audit pairs).
#
# ``VERTEX_ALT_MERGE_TOL_M`` no longer splits node ids (the pre-ruling
# "clean wall" twin path is gone); it remains the threshold separating
# a silent consensus merge (claims within it = solver/rounding noise)
# from a level change that upstream must resolve as geometry — the
# heal/blend passes and the stacked-conflict wall emitter reason with
# it, and ``tools/check_grade.py::_check_stacked_nodes`` enforces the
# structural invariant on the emitted OSM (cap 0).
VERTEX_ALT_MERGE_TOL_M = 1.0

# PAVEMENT-NODE RULE (user 2026-07-09): a pavement edge keeps a node
# every ~60 m so the elevation solver holds the edge at its solved
# grade — a longer chord lets the pavement sag visibly between distant
# nodes.  The emit-time decimation must never leave an airside-pavement
# chord longer than this (a fine-densified straight run collapsed a CYXY
# junction edge to 1,056 m before this bound was enforced across the
# whole run, not just per single-vertex removal).
PAVEMENT_NODE_MAX_CHORD_M = 60.0


def vertex_bucket(x: float, y: float,
                  tol: float = SHARED_VERTEX_TOL_M) -> "tuple[int, int]":
    """Quantize a meter-space point to a discrete vertex-bucket key.

    Two coordinates within ``tol`` metres of each other hash to the
    same bucket — used to treat vertices on adjacent shapes that
    should share a node as a single logical point.

    THE single source of truth for discrete vertex bucketing.  This
    same formula was previously duplicated as
    ``elevation._corner_elevation_bucket``,
    ``seam_anchors._bucket_key``, and inline ``round(x * 2.0)`` in
    ``junction_rules`` — all now delegate here so the scheme can
    never silently diverge.  (``round(x / 0.5)`` ≡ ``round(x * 2.0)``
    exactly in IEEE-754, so this consolidation is bit-for-bit
    behaviour-preserving.)
    """
    return (int(round(x / tol)), int(round(y / tol)))


def corner_alts_from_high_low(eh: float, el: float) -> "list[float]":
    """Per-corner altitudes for a 4-corner sloped rect, in the
    canonical ``[high, low, low, high]`` corner order (corners 0,3 at
    the high end; 1,2 at the low end).

    THE single source of truth for the ``[H, L, L, H]`` convention
    shared by rect emission, seam-anchor conversion, the OSM
    tag-writer, and the runway/junction altitude packers — previously
    open-coded as ``[eh, el, el, eh]`` in ~half a dozen places.
    Returns the OPEN (4-element) ring; callers append the closing
    repeat themselves where they need the 5-element closed form.
    """
    return [float(eh), float(el), float(el), float(eh)]


def high_low_from_corner_alts(corner_alts) -> "tuple[float, float]":
    """Inverse of :func:`corner_alts_from_high_low`: recover
    ``(high, low)`` from a 4-corner ``[H, L, L, H]`` altitude list by
    averaging each end's corner pair (tolerant of small per-corner
    drift introduced by the per-node consensus / solver)."""
    a = list(corner_alts)
    return ((a[0] + a[3]) / 2.0, (a[1] + a[2]) / 2.0)




# ──────────────────────────────────────────────────────────────────
# Role-tag vocabulary
# ──────────────────────────────────────────────────────────────────
ROLE_RUNWAY = "runway"
ROLE_PRIMARY_PARALLEL = PS.ROLE_PRIMARY_PARALLEL
ROLE_SECONDARY_PARALLEL = PS.ROLE_SECONDARY_PARALLEL
ROLE_STUB = PS.ROLE_STUB
ROLE_CROSS_CONNECTOR = PS.ROLE_CROSS_CONNECTOR
ROLE_APRON = PS.ROLE_APRON
# Building pads: terminals, hangars, towers — any flat fixed-floor
# structure the surrounding apron grades to.  Renamed from
# ROLE_TERMINAL (value "terminal") per user 2026-06-12; read paths
# (ROLE_GRADE_LIMITS, compare-target loader) keep a legacy
# "terminal" alias for pre-rename patches on disk.
ROLE_BUILDING = "building"
ROLE_JUNCTION = "junction"
# A junction at the intersection of two runways (user 2026-05-18).
# Created by ``_resolve_runway_crossings`` when overlapping runway
# segments are merged into a multi-directional sloping polygon.
# Distinct from ``ROLE_JUNCTION`` because:
#   * Its corners come from runway geometry (a SOURCE for adjacent
#     shapes), not from row-110 + rect-difference.
#   * It must NOT be reclassified to apron — the crossing IS the
#     centerline of two runways.
#   * Grade enforced per-runway-axis (per the junction rule).
ROLE_RUNWAY_CROSSING = "runway_crossing"
ROLE_BOUNDARY = "boundary"
# Tunnel portals: ``tunnel_ramp`` is a sloped 4-corner rect from
# outside-DEM down to apt-elev-6m at the portal; ``retaining_wall``
# is a flat polygon at apt-elev forming the U-shape around the
# portal LOW end.
ROLE_TUNNEL_RAMP = "tunnel_ramp"
ROLE_RETAINING_WALL = "retaining_wall"
# Groundside terminal pavement (curbside / drop-off / parking) —
# emitted with per-vertex DEM altitudes and a 0.1 m gap from the
# terminal building so it follows local terrain instead of being
# flattened to airside-apron elevation.
ROLE_GROUNDSIDE_PAVEMENT = "groundside_pavement"
# Ground-vehicle service road (apt.dat 1206 truck route OR OSM small
# road) that runs as a DEDICATED strip outside aircraft pavement.  A
# sloped 4-corner rect graded along its axis at 4% (cars handle steeper
# terrain than aircraft); helps ramp between apron and DEM elevations.
# Where a 1206 / OSM road instead crosses an aircraft movement area
# (apron / taxiway) it is NOT emitted as a service_road — the stricter
# aircraft grade rules of that surface apply (session 47).
ROLE_SERVICE_ROAD = "service_road"
# Junction polygon of the ground-vehicle service-road network (fills the
# bends / intersections between service_road rects, same way ROLE_JUNCTION
# fills the taxi network).  Graded all-direction at 4% (car logic).
ROLE_SERVICE_JUNCTION = "service_junction"
# Wingtip / RESA clearance cuts: terrain-following node_altitudes
# polygons emitted alongside taxiways and runways (and off runway
# ends) by ``clearance.emit_surface_clearance_cuts``.  They CUT
# terrain that rises above the adjacent surface edge within the
# lateral clearance band / runway-end safety area down to a ramped
# ceiling.  Like ROLE_BOUNDARY they trace/override terrain, so they
# carry no within-shape grade rule.
ROLE_TAXIWAY_CLEARANCE = "taxiway_clearance"
ROLE_RUNWAY_CLEARANCE = "runway_clearance"
# Adjacent-ground graded strip (adjacent_ground.py, gate
# ADJACENT_GROUND_LAW_ENABLED): terrain-following node_altitudes polygons
# emitted alongside pavement — the LATERAL generalization of the
# runway-end skirt.  Like the clearance roles it traces/overrides terrain
# to the lawful corridor bound, so it carries NO within-shape pavement
# grade rule (ROLE_GRADE_LIMITS None) and is NOT airside pavement.
ROLE_GRADED_STRIP = "graded_strip"

# Weld-DONOR roles (user rulings 2026-07-09/2026-07-17): the pavement
# families a SOFT terrain strip may ADOPT a coincident authority value
# from — at the emit consensus (``to_osm``'s strip-adoption branch) and
# at adjacent_ground's static-edge weld alike.  Designed splits
# (buildings, service roads, terminals, groundside, bridge plates) are
# NOT donors: a strip meeting them keeps its own lawful value — a
# designed wall — because adopting a foreign authority corner minted
# near-vertical tears inside the strip (measured CYXY: a building-pad
# corner at 705.5 welded into a 714.6 band edge = a 9 m face over
# 0.9 m; the DEM-free tear sentinel flags exactly this class).  THE
# single source — adjacent_ground imports this set.
WELD_DONOR_ROLES = frozenset((
    ROLE_RUNWAY, ROLE_RUNWAY_CROSSING,
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_APRON,
    ROLE_RUNWAY_CLEARANCE, ROLE_TAXIWAY_CLEARANCE,
))
# Object-derived bridge terrain (feature B, gate O4_OBJECT_BRIDGE_TERRAIN,
# user ruling R12 — geometry-phase shapes, solver-immutable values):
# ``bridge_trench`` is the depressed under-deck corridor floor of a
# DECK_CARRIED span (flat at the law floor = the anchor-terrain datum,
# amendment A10); ``bridge_causeway`` is the flat approach plate between
# the abutment lip and the pavement the pack cut short of it (flat at the
# deck-end law elevation).  Both are born at layout time with per-vertex
# ``node_altitudes`` from the grade law and are never touched by the
# solver or any mutation pass — flat by law, no within-shape grade rule.
ROLE_BRIDGE_TRENCH = "bridge_trench"
ROLE_BRIDGE_CAUSEWAY = "bridge_causeway"

# Feature A (O4_OBJECT_TUNNEL_TERRAIN, docs/object_terrain_features_spec.md
# section 3.3 + amendment A1).  The whole-body tunnel trench floor pan (at
# the law floor) and its rim collar (at the datum) — born at layout time
# with per-vertex ``node_altitudes`` from ``grade_law.tunnel_trench_*`` and
# shipped per-node (flat-by-law, decimation-exempt, LAW-tier weld, no
# within-shape grade rule), exactly like the bridge plates ABOVE with ONE
# deliberate difference: it is OFF-PAVEMENT terrain (ruling R2 subtracts the
# airside pavement from the body before birth), so it is NOT a pavement
# solver member (absent from ``solver_primitives.PAVEMENT_ROLES``).  That
# keeps the deep floor from dragging adjacent airside pavement down through
# the one-solve — the trench value still WINS at any shared vertex (LAW
# tier), the rim welds to the surrounding terrain, and the vertical drop is
# the R2 node-split wall.  (Measured: reusing ROLE_BRIDGE_TRENCH pulled 30 %
# of EGLL airside pavement down, up to 8 m near tunnels; this role fixes it.)
ROLE_TUNNEL_TRENCH = "tunnel_trench"

# SOFT RECEIVERS (weld ruling 2026-07-09): terrain-grading roles whose
# values ADOPT from pavement / solver-owned shapes at shared vertices —
# value authorities never move (user ruling: the PAVEMENT value always
# wins at a pavement node).  Single source for the emit consensus in
# ``to_osm`` and the emitters' weld-value adoption.
SOFT_RECEIVER_ROLES = frozenset({
    ROLE_GRADED_STRIP, ROLE_RUNWAY_CLEARANCE,
    ROLE_TAXIWAY_CLEARANCE, ROLE_RETAINING_WALL, ROLE_BOUNDARY,
})

AEROWAY_FOR_ROLE = {
    ROLE_RUNWAY: "runway",
    ROLE_PRIMARY_PARALLEL: "taxiway",
    ROLE_SECONDARY_PARALLEL: "taxiway",
    ROLE_STUB: "taxiway",
    ROLE_CROSS_CONNECTOR: "taxiway",
    ROLE_JUNCTION: "taxiway",
    ROLE_RUNWAY_CROSSING: "runway",
    ROLE_APRON: "apron",
    ROLE_BUILDING: "building",
    ROLE_BOUNDARY: "aerodrome",
    ROLE_TUNNEL_RAMP: "taxiway",
    ROLE_RETAINING_WALL: "building",
    ROLE_GROUNDSIDE_PAVEMENT: "apron",
    ROLE_SERVICE_ROAD: "taxiway",
    ROLE_SERVICE_JUNCTION: "taxiway",
    ROLE_TAXIWAY_CLEARANCE: "aerodrome",
    ROLE_RUNWAY_CLEARANCE: "aerodrome",
    ROLE_GRADED_STRIP: "aerodrome",
    # Bridge terrain plates (feature B, R12) override terrain like the
    # clearance / graded-strip features — no taxiable aeroway semantics.
    ROLE_BRIDGE_TRENCH: "aerodrome",
    ROLE_BRIDGE_CAUSEWAY: "aerodrome",
    ROLE_TUNNEL_TRENCH: "aerodrome",
}


def _rect_short_edge_width_m(polygon) -> float | None:
    """Measured pavement width (m) of a taxiway shape = the SHORT side of
    its minimum rotated rectangle.  Fallback for taxi networks that carry
    no apt.dat code letter (OSM-sourced)."""
    if polygon is None or polygon.is_empty:
        return None
    try:
        mrr = min_rotated_rect(polygon)
        pts = list(mrr.exterior.coords)
    except _GEOM_EXC:
        return None
    if len(pts) < 4:
        return None
    sides = [math.hypot(pts[i + 1][0] - pts[i][0],
                        pts[i + 1][1] - pts[i][1])
             for i in range(min(4, len(pts) - 1))]
    return min(sides) if sides else None


def taxi_shape_code_letter(layout, shape) -> str | None:
    """ICAO design code LETTER ("A".."F") for a taxiway-family ``shape``,
    or ``None`` when the size-dependent grade cap does not apply (the gate
    is off, or the shape is not a sized taxiway role).

    The ICAO size is MEASURED from the rect's short-edge width (user
    2026-06-29: size is a property of the geometry, not a name→letter table).
    Shared by the solver (cap selection at solve time) and the OSM emitter (the
    ``code_letter`` tag the validator reads back) so all three stay in lockstep."""
    if not TAXI_GRADE_BY_WIDTH:
        return None
    if shape.role not in TAXI_GRADE_WIDTH_ROLES:
        return None
    width = _rect_short_edge_width_m(shape.polygon)
    return taxiway_code_letter(width) if width is not None else None


# ──────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────

@dataclass
class BuiltShape:
    """A single emitted shape: polygon + classification tags.

    Polygons live in meter space anchored at the layout's origin.
    ``ref`` is optional (runway designator, taxi ref from OSM, or
    generated label).  ``source_axis`` is kept on taxi rects for
    elevation sampling along their axis.

    Phase-2 elevation: exactly one of these options is set at any
    time:
      * all four None (no elevation yet)
      * only ``altitude`` set (flat polygon at that elevation, m)
      * ``altitude_high`` + ``altitude_low`` set (linearly sloped
        between the two parallel edges).  Rects use this.
      * ``node_altitudes`` set (per-vertex elevation list, one
        value per ring vertex INCLUDING the closing repeat —
        i.e. len(node_altitudes) == len(closed_ring_nids)).
        Used for triangulated junction polygons that slope in
        more than one direction.
    Runway segments carry altitude_high/low per the legacy patch
    convention.
    """
    polygon: Polygon
    role: str
    ref: str = ""
    source_axis: LineString | None = None
    altitude: float | None = None
    altitude_high: float | None = None
    altitude_low: float | None = None
    node_altitudes: list[float] | None = None
    # OSM ``bridge=yes`` flag.  Set on taxi rects whose source
    # OSM way is tagged as a bridge — see ``_emit_taxi_bridges``.
    is_bridge: bool = False
    # Rect end-cap flag (gate RECT_END_CAPS).  Set on the small flat
    # junction strips carved off a sloping rect's open flat ends at
    # rect-build time (rect_end_caps.py).  They are INTENTIONAL geometry
    # that must persist (so the centerline-spine welds onto the cap's
    # soft edge, 2 m clear of the rect's sloping edge), so the
    # sliver-junction merge pass must NOT absorb them back.
    is_rect_cap: bool = False
    # Set by ``_reclassify_apron_junctions`` when a ROLE_JUNCTION shape is
    # flipped to ROLE_APRON by the boundary-distance rule.  The flip is
    # whole-shape (one far corner beyond the cap condemns the entire
    # polygon), so downstream splitters (apron neck-split) re-evaluate each
    # piece of a flagged parent: pieces that hug the taxi spine return to
    # ROLE_JUNCTION instead of inheriting apron and its stand-apron grading
    # treatment.  Born-apron shapes never carry the flag, so genuine stand
    # aprons are never promoted.
    reclassified_from_junction: bool = False
    # Set on pieces minted by the apron route-proximity CUT (pipeline,
    # user 2026-07-06 50 m ruling).  A cut piece is a deliberate
    # re-partition of ALREADY-KEPT pavement — a near-band fragment can
    # individually fall below the off-source residue thresholds even
    # though its parent passed (KCLT junction #255, 1.9 k m² dropped),
    # so ``_drop_off_source_residue`` must not judge it.
    from_route_proximity_cut: bool = False
    # USER RULING 2026-07-06: a service road / service junction that
    # SHARES AN EDGE with an apron follows the APRON grading rules —
    # the road is part of the stand surface there, and a 4-5 % ramp
    # tearing along a 1 % stand edge is exactly the weld-conflict class.
    # Set by the pipeline's apron-edge adoption pass; consumed by the
    # solver cap resolvers and emitted as ``o4_grade_law='apron'`` for
    # the validator (both readers stay lockstep).
    adopts_apron_grade: bool = False
    # USER RULING 2026-07-07 (durable, STATUS part 29 item 4): the PORTION
    # of a service road INSIDE or SHARING A LONG EDGE with a TAXIWAY
    # follows the more limiting (taxiway) grade law — 1.5 % instead of the
    # road's 5 %.  Mirrors ``adopts_apron_grade`` exactly (portion-based,
    # split at the taxiway-adjacency band).  ``adopted_taxi_letter`` carries
    # the adjacent taxiway's ICAO code letter so the solver + validator can
    # apply the letter-aware cap (``taxi_grade_cap_for_letter``); None → the
    # uniform 1.5 % ``TAXI_MAX_GRADE``.  APRON (1 %) is more limiting than
    # taxi (1.5 %), so a road already adopting apron is left alone — this
    # flag is set only on portions NOT already apron-adopted.
    adopts_taxi_grade: bool = False
    adopted_taxi_letter: str | None = None
    # Runway DE-SEGMENTATION (O4_RUNWAY_SINGLE_POLY, docs/
    # runway_single_polygon_plan.md): this ROLE_RUNWAY shape is ONE ring
    # per runway ref built from the persisted FAA profile (long-edge
    # vertices at every profile station, per-vertex altitudes) instead of
    # a chain of abutting sub-rects.  Consumers that special-case the
    # segmented form (crossing resolution, cross-edge crown tenting,
    # apron-merge whole-piece drops) key off this flag.
    from_single_poly: bool = False



@dataclass
class PavementLayout:
    icao: str
    anchor: tuple[float, float]          # (lat0, lon0)
    shapes: list[BuiltShape] = field(default_factory=list)
    # ancillary:
    airport_boundary: Polygon | None = None
    runway_union: Polygon | None = None
    # Source pavement union (apt.dat row-110 ⊕ DSF, before runway
    # subtraction), in this layout's meter frame.  Set by the pipeline;
    # used by build-time verification's per-shape source-adjacency check
    # (every emitted pavement shape must rest on real source pavement).
    source_pavement_union: Polygon | None = None
    # Path to the apt.dat file the layout was built from.  Used by
    # the bridge-detection step to walk the same scenery pack's
    # DSF and check for taxi-bridge OBJ placements.
    apt_dat_path: str | None = None
    # Full apt.dat taxi-network centerline set (preserved before
    # rect / junction emission consumes some into absorbed
    # polygons).  Used by the apron-reclassification pass to
    # decide which junctions have a centerline running through
    # them — surviving rect ``source_axis`` covers only ~40 % of
    # the original centerlines at SPJC because the rest were
    # absorbed into junction polygons, so the reclassification
    # would otherwise misflag legitimate junctions as aprons.
    # Stored as ``apt_dat_reader.TaxiCenterline`` (connectivity routes carrying
    # per-segment ICAO size + ``is_service``; name is a label only).
    apt_taxi_centerlines: list = field(
        default_factory=list)
    # Ground-vehicle (service-road) centerlines from apt.dat row 1206,
    # as ``(LineString, route_name)`` in meter space — drive the 4 %-grade
    # ``service_road`` rects.  Empty when the block has no 1206 network.
    apt_service_centerlines: list[tuple[LineString, str]] = field(
        default_factory=list)
    # apt.dat row-110 pavement polygon vertices, in meter space.
    # Junction polygons are built as
    # ``pav_union.difference(rects)`` and inherit their perimeter
    # vertices from these (where the perimeter follows row-110)
    # and from rect corners (where it abuts a rect).  Captured
    # here so the source-attribution test can recognise them as
    # legitimate vertex sources rather than densification orphans.
    apt_pavement_vertices: list[tuple[float, float]] = field(
        default_factory=list)
    # Union of apt.dat row-110 pavement polygons' boundaries in
    # meter space.  Junction perimeters that follow the row-110
    # pavement edge can land at any point ALONG these segments
    # (not only at the segment endpoints in
    # ``apt_pavement_vertices``).  Captured here so the
    # source-attribution test can recognise mid-edge points as
    # legitimate inheritances from row-110 rather than orphan
    # densification.
    apt_pavement_boundary: BaseGeometry | None = None
    # Canonical-point registry shared across every pass that creates
    # or modifies a polygon vertex.  Per user 2026-05-18: each
    # shared corner across multiple shapes must resolve to ONE
    # canonical (x, y) — exact-equality coordinates — so
    # ``pav_union.difference(rects)`` and the OSM emitter's vertex
    # bucketing produce a single node ID per real-world meeting
    # point.  See ``canonical_points.CanonicalPointRegistry`` and
    # the rect-builder seeding in ``pipeline.py``.
    canonical_points: CanonicalPointRegistry | None = None

    # Elevation-inset provenance read off the DEM the elevation solve graded
    # against (``provenance.dem_provenance_from_dem`` result: which airport-
    # elevation insets baked into that surface, or the loud RAW marker).  Set
    # by ``pipeline`` at solve time; consumed by ``to_osm`` to stamp the patch
    # and by the driver to log one provenance line per airport.  None when no
    # elevation solve ran (the patch then reports RAW — graded on base DEM).
    dem_inset_provenance: dict | None = None

    # The provenance record ``to_osm`` assembled for this build, cached so the
    # driver logs its one-line summary from the same truth it stamped.  None
    # until ``to_osm`` runs with provenance enabled.
    _provenance_record: dict | None = None

    # ---- coordinate helpers ------------------------------------------
    # COORDINATE-ORDER CONVENTION (read before editing geometry code):
    #   * "xy"  = local METRES from ``anchor``, order (x=east, y=north).
    #            All shape ``polygon`` coords and per-vertex work are xy.
    #   * "ll"  = geographic, order (lat, lon) — what m_to_ll RETURNS
    #            and ll_to_m TAKES.
    #   * shapely geometries built from lat/lon use (x=lon, y=lat) —
    #            the OPPOSITE order — e.g. ``Polygon([(lon, lat), ...])``
    #            and ``_projection.to_m(lon, lat)``.  ``_sample_dem``
    #            also takes (lat, lon) but indexes the DEM as
    #            (lon-tile_lon, lat-tile_lat).
    # The order flips at each ll<->shapely boundary; keep ll tuples
    # named ``(lat, lon)`` and metre tuples ``(x, y)`` so the flip is
    # always visible at the call site.
    def m_to_ll(self, x: float, y: float) -> tuple[float, float]:
        lat0, lon0 = self.anchor
        cos0 = math.cos(math.radians(lat0))
        lon = lon0 + math.degrees(x / (R_EARTH * cos0))
        lat = lat0 + math.degrees(y / R_EARTH)
        return lat, lon

    def ll_to_m(self, lat: float, lon: float) -> tuple[float, float]:
        lat0, lon0 = self.anchor
        cos0 = math.cos(math.radians(lat0))
        x = math.radians(lon - lon0) * R_EARTH * cos0
        y = math.radians(lat - lat0) * R_EARTH
        return x, y

    # ---- serialization -----------------------------------------------
    def to_osm(self, path: str) -> None:
        """Emit to a JOSM-readable OSM file with shared node IDs.

        Vertices within ``SHARED_VERTEX_TOL_M`` are assigned the same
        node id, matching the target-OSM convention.

        Two invariants enforced at emit time (user 2026-05-18):

        * ``Same-XY → same-altitude``: two vertices sharing an XY
          bucket but disagreeing on altitude by more than
          ``VERTEX_ALT_MERGE_TOL_M`` get DIFFERENT node IDs —
          preserving real walls / cliffs / grade transitions
          instead of collapsing them into a vertex with two
          altitudes.
        * ``Shared-corner altitude consensus``: when multiple shapes
          DO share a node (their altitudes were within the merge
          tolerance), each shape's emitted altitude tag at that
          corner is rewritten to the mean of all contributing
          shapes' altitudes.  Result: no cross-shape proximity
          tear in the emitted OSM.  Shapes whose corners drift off
          their original flat / sloping-rect pattern fall back to
          ``node_altitudes`` so the per-corner consensus is
          preserved.
        """
        # Canonical-point key → list of (node_id, claimed_altitude).
        # Per user 2026-05-18: the OSM emitter uses the same shared
        # CanonicalPointRegistry as the solver, so vertex matching
        # is proximity-based (single source of truth) rather than
        # discrete-bucket-based.  Two corners 0.002 m apart that
        # would have landed in adjacent discrete buckets now
        # resolve to the same canonical point.  Altitude-aware
        # sub-grouping inside each canonical point preserves the
        # ``VERTEX_ALT_MERGE_TOL_M`` rule (wall / cliff separation
        # when Δalt > tol).
        registry = self.canonical_points
        if registry is None:
            # Defensive: a layout constructed outside the pipeline
            # (tests) may lack a registry.  Build one on the fly
            # so this method is callable independently.
            from .canonical_points import CanonicalPointRegistry
            registry = CanonicalPointRegistry(
                tol_m=SHARED_VERTEX_TOL_M)
        xy_to_nodes: dict[tuple[float, float],
                          list[tuple[int, float | None]]] = {}
        node_id_to_ll: dict[int, tuple[float, float]] = {}
        # Accumulate every altitude contributed to each node so the
        # post-intern consensus pass can average them.
        node_id_to_alts: dict[int, list[float]] = {}
        # VALUE-AUTHORITY claims (weld ruling 2026-07-09): altitudes
        # contributed by pavement / solver-owned shapes.  A node with any
        # authority claim takes the mean of the AUTHORITY claims only —
        # terrain-grading strips (graded_strip / clearance / skirt /
        # retaining_wall / boundary) now WELD onto pavement rings, and a
        # plain all-claims mean would let a strip's near-miss value MOVE
        # a runway ring vertex (the A2 doctrine at emit: authorities
        # never adopt; soft receivers adopt).
        node_id_to_authority_alts: dict[int, list[float]] = {}
        # LAW-VALUE claims (feature B): the object-bridge plates carry
        # grade-law constants (deck-end / corridor-floor elevations) —
        # a node with a law claim takes the LAW value; other authority
        # claims (approach-rect corners welded onto the causeway lip)
        # yield to it, exactly as soft claims yield to authorities
        # (post-merge audit delta: lip nodes averaged to 166.66-166.93
        # against the 167.00 law).
        node_id_to_law_alts: dict[int, list[float]] = {}
        # SKIRT-VALUE claims (runway-end skirt, 2026-07-10): the
        # runway-end skirt carries its OWN edge law (grade_law
        # .RUNWAY_END_SKIRT_MAX_DOWN_GRADE — the fill-only bounded
        # down-grade off a runway end).  It is a soft receiver against
        # pavement / solver AUTHORITIES (it adopts the pavement corner),
        # but among purely-soft claims it must WIN: where a runway-end
        # skirt shares a node with an adjacent-ground graded strip (both
        # SOFT), the plain all-soft mean pulls the skirt's level band row
        # off its floor toward the strip's lower terrain-follow value,
        # minting a mid-row valley that violates the skirt edge law
        # (CYXY skirt #271: skirt 693.1 + strip 692.3 averaged to 692.7,
        # a 0.4 m drop over a 2 m edge = 20 %).  The documented ruling is
        # that runway ENDS are out of scope for adjacent-ground — the
        # skirt law owns them (adjacent_ground.py) — so the strip yields
        # to the skirt at a shared node, exactly as soft claims yield to
        # authorities and authorities yield to law claims.  The strip
        # still welds (it references the same node, so it adopts the
        # skirt value): no cross-shape tear, and the strip carries no
        # edge law of its own to violate.
        node_id_to_skirt_alts: dict[int, list[float]] = {}
        _LAW_VALUE_ROLES = frozenset({
            ROLE_BRIDGE_TRENCH, ROLE_BRIDGE_CAUSEWAY, ROLE_TUNNEL_TRENCH,
        })
        _SOFT_RECEIVER_ROLES = SOFT_RECEIVER_ROLES
        current_shape_is_soft = [False]
        current_shape_is_law = [False]
        current_shape_is_skirt = [False]
        next_nid = [-1]

        def _record_claim(nid: int, alt: float) -> None:
            node_id_to_alts.setdefault(nid, []).append(alt)
            if not current_shape_is_soft[0]:
                node_id_to_authority_alts.setdefault(
                    nid, []).append(alt)
            if current_shape_is_law[0]:
                node_id_to_law_alts.setdefault(nid, []).append(alt)
            if current_shape_is_skirt[0]:
                node_id_to_skirt_alts.setdefault(nid, []).append(alt)

        def _intern(x: float, y: float,
                    alt: float | None = None) -> int:
            # NO-STACKED-NODES HARD MERGE (owner ruling 2026-07-19):
            # one canonical point = ONE node id, always.  Every claim
            # joins the node and the consensus pass resolves the value
            # by precedence (law > authority > skirt > soft mean) —
            # this subsumes the former altitude sub-grouping, the
            # strip donor-gated adoption, and the fresh-twin "clean
            # wall" path (all three could mint or preserve a second
            # node at the same coordinate, which renders as a bare
            # near-vertical mesh tear).  Level changes that must NOT
            # be averaged away are resolved UPSTREAM as horizontal
            # wall geometry before interning ever sees them
            # (``emit_stacked_conflict_walls``).
            key = registry.get_or_add(float(x), float(y))
            existing = xy_to_nodes.get(key)
            if existing:
                nid = existing[0][0]
                if alt is not None:
                    _record_claim(nid, alt)
                return nid
            nid = next_nid[0]
            next_nid[0] -= 1
            xy_to_nodes[key] = [(nid, alt)]
            # Use the CANONICAL coordinates (not the input) so all
            # nodes referencing this canonical point produce the
            # exact same lat/lon in the OSM file.
            node_id_to_ll[nid] = self.m_to_ll(key[0], key[1])
            if alt is not None:
                node_id_to_alts[nid] = [alt]
                if not current_shape_is_soft[0]:
                    node_id_to_authority_alts[nid] = [alt]
                if current_shape_is_law[0]:
                    node_id_to_law_alts[nid] = [alt]
                if current_shape_is_skirt[0]:
                    node_id_to_skirt_alts[nid] = [alt]
            return nid

        def _ring_to_nids(ring_coords, ring_elevs=None):
            """Build a closed-ring nid list from coords.

            Returns ``(nids, elevs_or_None)``.  ``elevs_or_None`` is
            an aligned per-vertex elevation list when ``ring_elevs``
            is provided (used for ``node_altitudes`` polygons);
            otherwise None.  Both share the same dedup logic so the
            element-count invariant survives.

            Defensive against upstream polygon-build bugs:

            * Drops consecutive duplicate nids (two ring vertices
              colliding in the SHARED_VERTEX_TOL_M bucket — would
              produce a zero-length edge that crashes downstream
              meshers).
            * Drops non-consecutive duplicate nids (a ring revisits
              the same node — figure-8 / self-touching polygon —
              keeping only the first occurrence).
            """
            coords = list(ring_coords)
            elevs = list(ring_elevs) if ring_elevs is not None else None
            if coords and coords[0] == coords[-1]:
                n_closed = len(coords)
                coords = coords[:-1]
                # Closing-repeat trim keyed on LENGTH, not value: a
                # closed elevation list has exactly one more entry than
                # the open ring.  The old value test
                # (``elevs[0] == elevs[-1]``) mis-trimmed an OPEN
                # ``[H, L, L, H]`` quad list (H == H) down to 3 entries,
                # and the misalignment guard below then dropped every
                # per-vertex value of the shape.
                if elevs is not None and len(elevs) == n_closed:
                    elevs = elevs[:-1]
            if len(coords) < 3:
                return None, None
            if elevs is not None and len(elevs) != len(coords):
                # Ring / per-vertex-value desync: some pass changed the
                # polygon without keeping ``node_altitudes`` aligned.
                # The values cannot be re-attached to vertices here, so
                # the ring is interned unvalued — but LOUDLY: unvalued
                # vertices ship without ``alt_abs`` and the mesh drops
                # them onto the raw DEM (the EGGW tunnel-plate collapse,
                # 2026-07-17).  Fix the mutating pass, never this warn.
                UI.vprint(1,
                    f"  [pav-builder] WARN: node_altitudes misaligned "
                    f"with ring ({len(elevs)} value(s) for "
                    f"{len(coords)} open vertices) — per-vertex "
                    f"altitudes dropped for this shape.")
                elevs = None
            if elevs is not None:
                nids = [_intern(x, y, elevs[k])
                        for k, (x, y) in enumerate(coords)]
            else:
                nids = [_intern(x, y) for (x, y) in coords]
            # Dedup any duplicate nid (consecutive OR not) AND collapse a
            # ZERO-LENGTH edge between two DISTINCT nids sharing the same
            # canonical coordinate.  ``_intern`` allocates two different
            # node ids at one canonical (x, y) when their altitudes differ
            # by more than ``VERTEX_ALT_MERGE_TOL_M`` (a wall / cliff), but
            # a WALL cannot have zero horizontal extent: two consecutive
            # ring vertices at the same XY emit a 0.00 m segment (KJQF
            # taxiway_clearance way -3870→-3871, Δalt 4.2 m at one point).
            # Triangle4XP degenerates on a zero-length constrained edge, so
            # drop the later of any two consecutive same-coordinate nids
            # (the first-kept altitude wins — matching the ``_intern``
            # first-match convention).
            seen: set = set()
            deduped_nids: list[int] = []
            deduped_elevs: list[float] = []
            prev_ll = None
            for k, nid in enumerate(nids):
                if nid in seen:
                    continue
                ll = node_id_to_ll.get(nid)
                if prev_ll is not None and ll is not None and ll == prev_ll:
                    # Zero-length edge (same canonical XY, different nid):
                    # collapse to the already-kept vertex.
                    continue
                seen.add(nid)
                deduped_nids.append(nid)
                prev_ll = ll
                if elevs is not None and k < len(elevs):
                    deduped_elevs.append(elevs[k])
            # Closing edge: if the last kept vertex sits at the first's
            # canonical coordinate (a zero-length CLOSING segment — the
            # wrap-around mirror of the guard above), drop it so the ring
            # closes on a real edge.
            while (len(deduped_nids) >= 4
                   and node_id_to_ll.get(deduped_nids[-1]) is not None
                   and node_id_to_ll.get(deduped_nids[-1])
                   == node_id_to_ll.get(deduped_nids[0])):
                deduped_nids.pop()
                if deduped_elevs:
                    deduped_elevs.pop()
            if len(deduped_nids) < 3:
                return None, None
            deduped_nids.append(deduped_nids[0])
            if elevs is not None:
                deduped_elevs.append(deduped_elevs[0])
                return deduped_nids, deduped_elevs
            return deduped_nids, None

        # Emit one simple way per shape (exterior ring only, with
        # all tags on that way).  Interior rings — which appear
        # on junction polygons that wrap around rect-shaped holes
        # — are dropped for X-Plane patch compatibility: the
        # Ortho4XP patch parser ([O4_Vector_Map.include_patches])
        # iterates ways only, so tags on an OSM multipolygon
        # relation never reach the outer way.  A junction ring
        # emitted without its holes will slightly overlap the
        # rects that used to punch those holes — X-Plane
        # triangulator handles the overlap by seed-region
        # processing; the rects' altitude_high/low tags prevail
        # where they cover.
        way_blocks: list[tuple[int, list[int], dict[str, str]]] = []
        # Pass-1 holding pen: each entry survives validation +
        # interning and waits for the consensus pass to write its
        # altitude tags from the per-node mean.
        # (s_idx, shape, ext_nids, shape_altitude, shape_node_altitudes)
        # — the per-shape altitude copies are carried so the tag-writing
        # pass below reads THIS shape's values, not a stale leftover from
        # the validation loop's last iteration.
        pending: list = []
        next_wid = [-10001]
        # Node ids removed by the per-shape sliver-corner repair below —
        # consumed by the chain-consistent post-pass after the loop.
        emit_removed_nids: set = set()
        for s_idx, s in enumerate(self.shapes):
            # Authority flag for the value-consensus pass: terrain
            # strips are SOFT receivers, everything else is a value
            # authority (see node_id_to_authority_alts above).
            current_shape_is_soft[0] = s.role in _SOFT_RECEIVER_ROLES
            current_shape_is_law[0] = s.role in _LAW_VALUE_ROLES
            # A runway-end skirt owns its edge law; among purely-soft
            # claims it wins over adjacent-ground strips (see
            # node_id_to_skirt_alts above).
            current_shape_is_skirt[0] = (
                s.role == ROLE_RUNWAY_CLEARANCE
                and s.ref == "runway_end_skirt")
            # Validate the polygon's geometry before emission.
            # Upstream pipeline stages (decomposition, seam-point
            # injection, shared-vertex enforcement) can occasionally
            # produce a self-touching ring that's geometrically
            # invalid; X-Plane's mesh builder crashes on these.
            poly = s.polygon
            if poly is None or poly.is_empty:
                continue
            # Local copies of the altitude representation.  to_osm is
            # a pure emitter — it must NOT mutate the input shapes
            # (a second to_osm call, or a caller that inspects
            # layout.shapes afterward, would otherwise see degraded
            # data).  The buffer(0) repair below degrades these
            # LOCAL copies only.
            shape_altitude = s.altitude
            shape_node_altitudes = s.node_altitudes
            shape_altitude_high = s.altitude_high
            shape_altitude_low = s.altitude_low
            if not poly.is_valid:
                try:
                    repaired = poly.buffer(0)
                    if (repaired.is_empty
                            or repaired.geom_type
                            not in ("Polygon", "MultiPolygon")):
                        continue
                    if repaired.geom_type == "MultiPolygon":
                        repaired = max(repaired.geoms,
                                       key=lambda g: g.area)
                    if (repaired.is_empty
                            or repaired.geom_type != "Polygon"):
                        continue
                    # node_altitudes from the original ring no longer
                    # aligns with the repaired ring; degrade to a
                    # flat polygon at the mean of the original
                    # vertex elevations to preserve emission.  Mutate
                    # only the local copies, never ``s``.
                    if shape_node_altitudes:
                        valid_elevs = [
                            e for e in shape_node_altitudes[:-1]]
                        if valid_elevs:
                            shape_altitude = round(
                                sum(valid_elevs) / len(valid_elevs),
                                1)
                        shape_node_altitudes = None
                    poly = repaired
                except _GEOM_EXC:
                    continue
            # Per-corner altitude derivation for shared-vertex
            # altitude-bucketing.  Source-shape attribution:
            #   * node_altitudes set → use directly
            #   * altitude set (flat polygon) → broadcast to every corner
            #   * altitude_high / altitude_low set (sloping rect, 4
            #     corners ring + closing) → corners 0,3 = high;
            #     corners 1,2 = low per ``_rect_from_axis_extended``
            #     convention
            # All three paths produce ring_elevs aligned with
            # ``poly.exterior.coords`` (including the closing
            # repeat).  Without per-corner altitudes the emitter
            # can't enforce the same-XY → same-altitude invariant.
            ring_elevs_input = shape_node_altitudes
            if ring_elevs_input is None:
                ext_coords_open = list(poly.exterior.coords)
                if (ext_coords_open
                        and ext_coords_open[0] == ext_coords_open[-1]):
                    ext_coords_open = ext_coords_open[:-1]
                n_open = len(ext_coords_open)
                if shape_altitude is not None:
                    ring_elevs_input = (
                        [float(shape_altitude)] * n_open
                        + [float(shape_altitude)])
                elif (shape_altitude_high is not None
                      and shape_altitude_low is not None
                      and n_open == 4):
                    eh = float(shape_altitude_high)
                    el = float(shape_altitude_low)
                    _open = corner_alts_from_high_low(eh, el)
                    ring_elevs_input = _open + [_open[0]]
            ext_nids, ext_elevs = _ring_to_nids(
                poly.exterior.coords,
                ring_elevs_input)
            if ext_nids is None:
                continue
            # Final validity check: rebuild the polygon from the
            # POST-DEDUP lat/lon coords AT THE PRECISION THE OSM
            # FILE WILL CONTAIN (.11f, ≈ 1 mm at the equator).
            # Polygons that are valid at full float precision can
            # become spike-vertex-on-non-adjacent-edge invalid
            # after this truncation; X-Plane's mesh builder
            # crashes on those.  Drop the whole shape rather than
            # ship a polygon X-Plane can't handle.
            try:
                latlon_ring = []
                for nid in ext_nids[:-1]:
                    lat, lon = node_id_to_ll[nid]
                    latlon_ring.append(
                        (float(f"{lat:.11f}"),
                         float(f"{lon:.11f}")))
                # Sliver-corner safety net + REPAIR: an interior angle
                # below SLIVER_ANGLE_THRESHOLD_DEG is a needle tip
                # Triangle4XP can't handle.  These can be BORN HERE —
                # canonical-point interning (~0.5 m buckets) plus the
                # .11f truncation sharpened a legal 9.3° corner on
                # KPHX's 400 704 m² terminal-core apron to 0.36°, and
                # the old drop-the-whole-shape response deleted the
                # entire terminal area from the patch.  Repair instead:
                # remove the needle-tip vertex (the spur is degenerate
                # — at 2° a 2.5 m spur tip sits <9 cm off the long
                # edge) and re-scan; drop the shape only if the ring
                # degenerates below 3 vertices or ends up invalid.
                work_nids = list(ext_nids[:-1])
                work_ring = list(latlon_ring)
                ring_m = [self.ll_to_m(lat, lon)
                          for (lat, lon) in work_ring]
                cos_thresh = math.cos(
                    math.radians(SLIVER_ANGLE_THRESHOLD_DEG))
                n_repaired = 0
                for _attempt in range(len(ring_m)):
                    m = len(ring_m)
                    if m < 3:
                        break
                    worst_vi = None
                    worst_cos = cos_thresh
                    for vi in range(m):
                        ax, ay = ring_m[(vi - 1) % m]
                        bx, by = ring_m[vi]
                        cx, cy = ring_m[(vi + 1) % m]
                        v1x, v1y = ax - bx, ay - by
                        v2x, v2y = cx - bx, cy - by
                        n1 = math.hypot(v1x, v1y)
                        n2 = math.hypot(v2x, v2y)
                        if n1 < 1e-9 or n2 < 1e-9:
                            continue
                        cos = (v1x * v2x + v1y * v2y) / (n1 * n2)
                        if cos > worst_cos:
                            worst_cos = cos
                            worst_vi = vi
                    if worst_vi is None:
                        break
                    # Record for the chain-consistent post-pass (weld
                    # ruling 2026-07-09): welded seams share vertex
                    # chains coordinate-exactly, so a needle removed
                    # from ONE way must also leave every partner way
                    # where it is near-collinear — else the two chains
                    # diverge by the needle height (≤9 cm) and the
                    # near-parallel constrained pair Ruppert-explodes
                    # the tile (CYXY weld bake: 26.7k → 1.55M airport
                    # triangles, hotspots at the repair sites).
                    # (A projection-onto-chord repair was measured and
                    # REJECTED 2026-07-09: most needle tips sit on a
                    # welded HOST edge whose ring does not reference
                    # the nid, and moving the tip pulls the chain off
                    # the host — near-parallel pairs 136 → 200.
                    # Removal keeps the surviving chord ON a straight
                    # host edge.)
                    emit_removed_nids.add(work_nids[worst_vi])
                    del ring_m[worst_vi]
                    del work_ring[worst_vi]
                    del work_nids[worst_vi]
                    n_repaired += 1
                if len(ring_m) < 3:
                    UI.vprint(1,
                        f"  [pav-builder] WARN: dropping "
                        f"sliver-corner polygon (role={s.role}, "
                        f"nids={len(ext_nids) - 1}): degenerated "
                        f"during needle repair.")
                    continue
                check_poly = Polygon(
                    [(lon, lat) for lat, lon in work_ring])
                if not check_poly.is_valid:
                    # The .11f quantization turned a full-precision-valid
                    # ring self-intersecting (a thin spur / tab whose
                    # sides cross at millimetre precision; the gate-1
                    # buffer(0) above only fires on full-precision-
                    # invalid polygons).  Dropping the whole shape leaves
                    # a hole in the pavement — LMML apron #152 was a
                    # 5 887 m² apron lost this way.  Repair with buffer(0)
                    # and keep the largest piece (the degenerate tab
                    # becomes a tiny sliver and is discarded).  Re-map the
                    # recovered ring to node ids: vertices that coincide
                    # with the pre-repair ring reuse their node id (and
                    # thus their consensus altitude + shared-vertex
                    # identity); buffer(0)'s self-touch vertex is interned
                    # fresh with the altitude of its nearest pre-repair
                    # vertex.
                    repaired_nids = None
                    try:
                        _rep = check_poly.buffer(0)
                        if _rep.geom_type == "MultiPolygon":
                            _rep = max(_rep.geoms, key=lambda g: g.area)
                        if (_rep.geom_type == "Polygon"
                                and not _rep.is_empty and _rep.is_valid):
                            _coord_to_nid = {
                                (round(la, 11), round(lo, 11)): work_nids[k]
                                for k, (la, lo) in enumerate(work_ring)}
                            _alt_for_nid = {}
                            if ext_elevs:
                                # ``ext_nids`` ↔ ``ext_elevs`` are the
                                # aligned pre-repair pairs; ``work_nids``
                                # may have had needle vertices removed
                                # above, so indexing ``ext_elevs`` by
                                # ``work_nids`` position mis-assigns
                                # every altitude past the first removal.
                                for k in range(min(len(ext_nids) - 1,
                                                   len(ext_elevs))):
                                    _alt_for_nid[ext_nids[k]] = ext_elevs[k]
                            _rep_open = list(_rep.exterior.coords)[:-1]
                            _mapped = []
                            for _lo, _la in _rep_open:   # (lon, lat)
                                _key = (round(_la, 11), round(_lo, 11))
                                _nid = _coord_to_nid.get(_key)
                                if _nid is None:
                                    # New self-touch vertex: NN altitude
                                    # from the pre-repair ring.
                                    _alt_nn = None
                                    _best = float("inf")
                                    for _k, (_la2, _lo2) in enumerate(
                                            work_ring):
                                        _d = ((_la - _la2) ** 2
                                              + (_lo - _lo2) ** 2)
                                        if (_d < _best
                                                and work_nids[_k]
                                                in _alt_for_nid):
                                            _best = _d
                                            _alt_nn = _alt_for_nid[
                                                work_nids[_k]]
                                    _xm, _ym = self.ll_to_m(_la, _lo)
                                    _nid = _intern(_xm, _ym, _alt_nn)
                                _mapped.append(_nid)
                            _seen2: set = set()
                            _dd: list[int] = []
                            for _nid in _mapped:
                                if _nid in _seen2:
                                    continue
                                _seen2.add(_nid)
                                _dd.append(_nid)
                            if len(_dd) >= 3:
                                _dd.append(_dd[0])
                                repaired_nids = _dd
                    except _GEOM_EXC:
                        repaired_nids = None
                    if repaired_nids is None:
                        try:
                            _area_m2 = abs(Polygon(ring_m).area)
                        except _GEOM_EXC:
                            _area_m2 = 0.0
                        UI.vprint(1,
                            f"  [pav-builder] WARN: dropping "
                            f"invalid polygon (role={s.role}, "
                            f"nids={len(ext_nids) - 1}, "
                            f"~{_area_m2:.0f} m²): "
                            f"X-Plane mesh builder would crash.")
                        continue
                    UI.vprint(1,
                        f"  [pav-builder] {s.role}: repaired invalid "
                        f"polygon at emit (buffer(0), {len(ext_nids) - 1}"
                        f"→{len(repaired_nids) - 1} verts; quantization "
                        f"self-intersection).")
                    pending.append((s_idx, s, repaired_nids,
                                    shape_altitude, shape_node_altitudes))
                    continue
                if n_repaired:
                    UI.vprint(1,
                        f"  [pav-builder] {s.role}: repaired "
                        f"{n_repaired} sliver corner(s) at emit "
                        f"(needle vertex removed, shape kept).")
                    ext_nids = work_nids + [work_nids[0]]
            except _GEOM_EXC:
                continue
            pending.append((s_idx, s, ext_nids,
                            shape_altitude, shape_node_altitudes))

        # ── Chain-consistent needle removal (weld ruling 2026-07-09) ──
        # A vertex the sliver-corner repair removed from one way must
        # also leave every OTHER way that passes through it NEAR-
        # COLLINEARLY (within the needle height, 0.09 m of its
        # neighbour chord) — welded seams share vertex chains, and a
        # one-sided removal diverges the two constrained chains into a
        # near-parallel sliver lens that Ruppert-refines to machine
        # epsilon.  A partner where the vertex is a REAL corner keeps
        # it (never deform genuine geometry; that seam keeps its
        # sliver — rare, logged by the epsilon-wedge tripwire).
        if emit_removed_nids:
            n_chain = 0
            for p_i, (s_idx, s, ext_nids, sa, sna) in enumerate(pending):
                open_nids = ext_nids[:-1]
                if not any(nid in emit_removed_nids for nid in open_nids):
                    continue
                kept = list(open_nids)
                changed = True
                while changed and len(kept) > 3:
                    changed = False
                    m = len(kept)
                    for k in range(m):
                        nid = kept[k]
                        if nid not in emit_removed_nids:
                            continue
                        la0, lo0 = node_id_to_ll[kept[(k - 1) % m]]
                        la1, lo1 = node_id_to_ll[nid]
                        la2, lo2 = node_id_to_ll[kept[(k + 1) % m]]
                        ax, ay = self.ll_to_m(la0, lo0)
                        bx, by = self.ll_to_m(la1, lo1)
                        cx, cy = self.ll_to_m(la2, lo2)
                        dx, dy = cx - ax, cy - ay
                        seg2 = dx * dx + dy * dy
                        if seg2 < 1e-12:
                            continue
                        t = ((bx - ax) * dx + (by - ay) * dy) / seg2
                        t = min(1.0, max(0.0, t))
                        perp = math.hypot(bx - (ax + t * dx),
                                          by - (ay + t * dy))
                        if perp <= 0.09:
                            del kept[k]
                            n_chain += 1
                            changed = True
                            break
                if len(kept) >= 3 and len(kept) < len(open_nids):
                    pending[p_i] = (s_idx, s, kept + [kept[0]], sa, sna)
            if n_chain:
                UI.vprint(1,
                    f"  [pav-builder] chain-consistent needle removal: "
                    f"dropped {n_chain} partner vertex(es) so welded "
                    f"seam chains stay identical.")

        # ── NID-LEVEL FINAL WELD (chain identity, 2026-07-09) ────
        # The canonical-point interning above can move a vertex
        # SIDEWAYS (0.5 m bucket) after the layout-level conformance
        # weld ran, and the needle/dedup repairs mutate ways per-
        # shape — so the T-vertex weld re-runs HERE, on the final nid
        # rings at the final coordinates: any nid lying on another
        # way's edge interior is inserted into that way.  Nid-level:
        # no new geometry is minted and the node's consensus altitude
        # rides along, so the constrained chains Triangle4XP sees are
        # identical by construction (one unwelded on-edge node
        # Ruppert-refines to ~10⁵-10⁶ tile triangles — measured at
        # CYXY 2026-07-09).  This must stay the LAST geometry-
        # affecting step of emission.
        _WELD_TOL_M = 0.005
        _cell_m = 1.0
        _nid_xy: dict[int, tuple[float, float]] = {}
        _grid: dict[tuple[int, int], list[int]] = {}
        for _p_i, (_si, _s, _enids, _sa, _sna) in enumerate(pending):
            for _nid in _enids[:-1]:
                if _nid in _nid_xy:
                    continue
                _la, _lo = node_id_to_ll[_nid]
                _xy = self.ll_to_m(_la, _lo)
                _nid_xy[_nid] = _xy
                _ck = (int(_xy[0] // _cell_m), int(_xy[1] // _cell_m))
                _grid.setdefault(_ck, []).append(_nid)
        _n_weld = 0
        for _p_i, (_si, _s, _enids, _sa, _sna) in enumerate(pending):
            open_nids = _enids[:-1]
            member = set(open_nids)
            out: list[int] = []
            changed = False
            m_open = len(open_nids)
            for k in range(m_open):
                n0 = open_nids[k]
                n1 = open_nids[(k + 1) % m_open]
                out.append(n0)
                ax, ay = _nid_xy[n0]
                bx, by = _nid_xy[n1]
                dx, dy = bx - ax, by - ay
                L2 = dx * dx + dy * dy
                if L2 < 1e-12:
                    continue
                L = math.sqrt(L2)
                # collect grid cells along the segment
                cand: set[int] = set()
                steps = max(1, int(L / _cell_m) + 1)
                for st in range(steps + 1):
                    qx = ax + dx * st / steps
                    qy = ay + dy * st / steps
                    c0 = int(qx // _cell_m)
                    c1 = int(qy // _cell_m)
                    for oi in (-1, 0, 1):
                        for oj in (-1, 0, 1):
                            cand.update(
                                _grid.get((c0 + oi, c1 + oj), ()))
                hits = []
                for nid in cand:
                    px, py = _nid_xy[nid]
                    t = ((px - ax) * dx + (py - ay) * dy) / L2
                    if t <= 0.0 or t >= 1.0:
                        continue
                    if t * L < _WELD_TOL_M or (1.0 - t) * L < _WELD_TOL_M:
                        continue
                    perp = abs((px - ax) * dy - (py - ay) * dx) / L
                    if perp >= _WELD_TOL_M:
                        continue
                    hits.append((t, nid))
                for _t, nid in sorted(hits):
                    # ZERO-LENGTH GUARD (Triangle4XP fatal, 2026-07-18):
                    # two coordinate-twin nodes can BOTH hit the same
                    # edge at the same parameter — inserting the second
                    # right after the first writes two consecutive ring
                    # references at one coordinate, which the mesh
                    # interns into a zero-length constrained segment
                    # (measured EGGW junction ring: twins 155.60/156.70
                    # from two adjacent-ground strips).  The mesh welds
                    # chains by coordinates, so ONE reference suffices —
                    # skip a hit coincident with the node just appended.
                    _last_xy = _nid_xy.get(out[-1]) if out else None
                    _hit_xy = _nid_xy.get(nid)
                    if (_last_xy is not None and _hit_xy is not None
                            and abs(_hit_xy[0] - _last_xy[0]) < 0.005
                            and abs(_hit_xy[1] - _last_xy[1]) < 0.005):
                        continue
                    # NO-STACKED-NODES (owner ruling 2026-07-19): the
                    # former value-twin branches here (the 2026-07-17
                    # donor-gated "designed wall" and the 2026-07-18
                    # soft strip-vs-strip tear guard) minted a second
                    # node id at the hit coordinate carrying the
                    # strip's own value — a stacked bare tear.  Both
                    # classes are now resolved UPSTREAM as geometry
                    # (``emit_stacked_conflict_walls`` retreats the
                    # strip edge and emits a retaining_wall face; the
                    # cross-strip seam blend levels soft↔soft steps),
                    # so the splice always references the ONE
                    # consensus node.
                    if nid in member:
                        # The way already passes through this node
                        # ELSEWHERE (a multi-way collinear seam that
                        # revisits the coordinate).  A repeated nid is
                        # a figure-8 the ring dedup forbids — but
                        # SKIPPING leaves the exact T-vertex that
                        # Ruppert-explodes (measured: one such seam =
                        # 673k triangles).  Insert a COORDINATE-TWIN
                        # nid instead: same canonical lat/lon and the
                        # same claims, so the mesh (which keys nodes
                        # by exact coordinates) welds the chains into
                        # one vertex while the OSM ring stays
                        # duplicate-free.
                        twin = next_nid[0]
                        next_nid[0] -= 1
                        node_id_to_ll[twin] = node_id_to_ll[nid]
                        if nid in node_id_to_alts:
                            node_id_to_alts[twin] = list(
                                node_id_to_alts[nid])
                        if nid in node_id_to_authority_alts:
                            node_id_to_authority_alts[twin] = list(
                                node_id_to_authority_alts[nid])
                        if nid in node_id_to_law_alts:
                            # Copy LAW claims too: the twin must land
                            # on the SAME consensus value as the
                            # original node (no-stacked-nodes: a twin
                            # is only legal because it shares the
                            # elevation).
                            node_id_to_law_alts[twin] = list(
                                node_id_to_law_alts[nid])
                        if nid in node_id_to_skirt_alts:
                            node_id_to_skirt_alts[twin] = list(
                                node_id_to_skirt_alts[nid])
                        _nid_xy[twin] = _nid_xy[nid]
                        out.append(twin)
                        member.add(twin)
                    else:
                        out.append(nid)
                        member.add(nid)
                    changed = True
                    _n_weld += 1
            if changed and len(out) >= 3:
                pending[_p_i] = (_si, _s, out + [out[0]], _sa, _sna)
        if _n_weld:
            UI.vprint(1,
                f"  [pav-builder] nid-level final weld: inserted "
                f"{_n_weld} on-edge node reference(s) into welded "
                f"partner ways.")

        # ── Consensus pass ──────────────────────────────────────
        # For each node id we now have every altitude any shape
        # contributed.  The consensus altitude is the mean — used
        # by the tag-writing pass below to enforce that every shape
        # touching a node agrees on the corner's altitude.  AUTHORITY
        # claims win (weld ruling 2026-07-09): when pavement / solver
        # shapes claimed the node, soft terrain-strip claims are
        # excluded from the mean, so a welded strip ADOPTS the pavement
        # corner value and never moves it.
        node_id_to_consensus: dict[int, float | None] = {}
        # Instrumentation (Slice B stage B1): count the nodes where the
        # runway-end-skirt precedence tier actually DECIDES the value — a
        # node with a skirt claim, NO law and NO authority claim, that
        # would otherwise take the all-soft mean (the #271 skirt-vs-strip
        # class).  Under the B1 gate this count is expected to be unchanged
        # from the legacy path for skirt-vs-STRIP nodes (the adjacent-ground
        # strip is not absorbed until B3, so it is still a soft claimant);
        # skirt-vs-PAVEMENT nodes never reach this tier (the pavement
        # authority claims them).  Published on the layout for the report.
        _skirt_tier_hits = 0
        for nid, alts in node_id_to_alts.items():
            law = node_id_to_law_alts.get(nid)
            authority = node_id_to_authority_alts.get(nid)
            skirt = node_id_to_skirt_alts.get(nid)
            # Priority: law > authority > runway-end skirt > all-soft
            # mean.  The skirt tier only bites when NO authority claimed
            # the node (a skirt-vs-strip weld among pure soft claims);
            # where pavement/solver claimed it, the skirt still adopts
            # the authority value as before.
            if not law and not authority and skirt and (
                    len(skirt) != len(alts)):
                # A skirt claim wins over OTHER soft claims present at the
                # node (the tier bites only when the skirt is not the sole
                # claimant — otherwise the mean equals the skirt value).
                _skirt_tier_hits += 1
            chosen = (law if law
                      else authority if authority
                      else skirt if skirt
                      else alts)
            if chosen:
                node_id_to_consensus[nid] = (
                    sum(chosen) / float(len(chosen)))
        self._skirt_consensus_tier_hits = (  # type: ignore[attr-defined]
            _skirt_tier_hits)

        # ── Unclaimed-node backfill (per-vertex preservation,
        # 2026-07-18) ────────────────────────────────────────────────
        # The nid-level final weld can insert a node NO shape ever
        # claimed an altitude for (its first-writer way interned it
        # without a value) into a value-carrying ring.  One
        # consensus-less node used to knock the ENTIRE way off the
        # per-node emission path (``have_all`` in the tag-writing pass
        # below), so a tunnel roof quad shipped with ``alt_abs`` on
        # only the 2-3 vertices other ways happened to claim and the
        # mesh dropped the rest onto raw DEM (EGGW +51-001,
        # 2026-07-17).  Give every unclaimed node of a value-carrying
        # way the ring-interpolated altitude between its nearest
        # claimed neighbours — the value the host edge carried where
        # the weld inserted the node.  A node ANY shape claimed keeps
        # its consensus untouched (the fill never overrides a claim),
        # so the law/authority/skirt tiers above are unaffected.
        _n_backfilled = 0
        for _si, _s, _enids, _sa, _sna in pending:
            _has_values = (_sa is not None or _sna is not None
                           or _s.altitude is not None
                           or _s.node_altitudes is not None
                           or (_s.altitude_high is not None
                               and _s.altitude_low is not None))
            if not _has_values:
                continue
            _open = _enids[:-1]
            _m = len(_open)
            if _m < 3:
                continue
            _missing = [k for k in range(_m)
                        if node_id_to_consensus.get(_open[k]) is None]
            if not _missing or len(_missing) == _m:
                # Nothing to fill, or nothing to fill FROM (a fully
                # unvalued ring — the misalignment warn above already
                # fired; the tag-writing fallback handles it).
                continue
            for _k in _missing:
                _nid = _open[_k]
                if node_id_to_consensus.get(_nid) is not None:
                    continue        # filled through an earlier way
                _dists = [None, None]   # (backward, forward)
                _vals = [None, None]
                for _side, _sgn in ((0, -1), (1, +1)):
                    _d = 0.0
                    _xy = self.ll_to_m(*node_id_to_ll[_nid])
                    for _step in range(1, _m):
                        _n2 = _open[(_k + _sgn * _step) % _m]
                        _xy2 = self.ll_to_m(*node_id_to_ll[_n2])
                        _d += math.hypot(_xy2[0] - _xy[0],
                                         _xy2[1] - _xy[1])
                        _xy = _xy2
                        _v2 = node_id_to_consensus.get(_n2)
                        if _v2 is not None:
                            _dists[_side] = _d
                            _vals[_side] = _v2
                            break
                if _vals[0] is not None and _vals[1] is not None:
                    _tot = (_dists[0] or 0.0) + (_dists[1] or 0.0)
                    _fill = (_vals[0] if _tot <= 1e-9
                             else _vals[0] + (_vals[1] - _vals[0])
                             * ((_dists[0] or 0.0) / _tot))
                elif _vals[0] is not None:
                    _fill = _vals[0]
                elif _vals[1] is not None:
                    _fill = _vals[1]
                else:
                    continue
                node_id_to_consensus[_nid] = _fill
                _n_backfilled += 1
        if _n_backfilled:
            UI.vprint(1,
                f"  [pav-builder] per-vertex backfill: interpolated "
                f"{_n_backfilled} unclaimed welded node(s) so no "
                f"value-carrying way loses its per-node emission.")

        def _corner_alt(nid: int) -> float | None:
            return node_id_to_consensus.get(nid)

        # ── CHAIN-AWARE FINAL DECIMATION (slice C, 2026-07-09) ────
        # After the welds/adoptions, pavement rings carry thousands of
        # 3D-REDUNDANT vertices (collinear in XY AND on the altitude
        # lerp — measured 4,470 of 4,687 mid-edge pavement vertices at
        # CYXY): emit decimation runs BEFORE the welds pin them via
        # shared references, and nothing decimated after.  A vertex is
        # removed ONLY when it is redundant in EVERY pending way that
        # references it, and then removed from ALL of them in the same
        # sweep — shared chains stay identical by construction (a
        # one-sided removal is the Ruppert lens class).  Real profile
        # nodes (crown drops, solver stations) fail the altitude-lerp
        # test and stay.  Tolerances per the 2026-07-09 precision
        # ruling (grading under pavement, no centimetre fidelity).
        _DEC_PERP_M = 0.02
        _DEC_ALT_M = 0.10
        # Max chord a removal may leave (pavement-node rule, module
        # constant PAVEMENT_NODE_MAX_CHORD_M).  The per-single-vertex
        # check below is necessary but NOT sufficient: a bulk sweep
        # removes an entire fine-densified straight run at once — each
        # vertex passes the check against its ~2 m immediate neighbours,
        # yet the run collapses to one chord far longer than the cap
        # (a CYXY junction edge reached 1,056 m).  The retention pass
        # after the redundancy scan enforces the cap over the whole run.
        _DEC_MAX_CHORD_M = PAVEMENT_NODE_MAX_CHORD_M
        _n_chord_retained = 0
        # LAW-PLATE protection (feature B): every coordinate referenced
        # by an object-bridge plate way is exempt from the sweep — the
        # plates' ~5 m densification is a deliberate Triangle4XP
        # constraint (post-merge audit delta: the sweep took the trench
        # ring 75 -> 17 nodes and the in-trench interpolation dipped
        # 1.09 m below the law floor at Murfreesboro).
        _law_plate_ll: set[tuple] = set()
        for _si, _s, _enids, _sa, _sna in pending:
            if getattr(_s, "role", None) in (ROLE_BRIDGE_TRENCH,
                                             ROLE_BRIDGE_CAUSEWAY,
                                             ROLE_TUNNEL_TRENCH):
                for _nid in _enids:
                    _law_plate_ll.add(node_id_to_ll[_nid])
        for _sweep in range(4):
            # Group by COORDINATE, not nid: coincident twin nids (wall
            # splits, the weld's coordinate-twins) must be removed
            # unanimously or kept unanimously — dropping one twin's
            # ways while another's keep the coordinate leaves a chord
            # passing exactly through the survivor (an exact T-vertex,
            # the lens class).
            _occ: dict[tuple, list[tuple[int, int, int]]] = {}
            _multi: set[tuple] = set()
            for _p_i, (_si, _s, _enids, _sa, _sna) in enumerate(pending):
                _open = _enids[:-1]
                _seen_local: set[tuple] = set()
                for _pos, _nid in enumerate(_open):
                    _ll = node_id_to_ll[_nid]
                    if _ll in _seen_local:
                        _multi.add(_ll)
                    _seen_local.add(_ll)
                    _occ.setdefault(_ll, []).append((_p_i, _pos, _nid))
            _removable_ll: set[tuple] = set()
            for _ll, _sites in _occ.items():
                if _ll in _multi:
                    continue
                _a1 = None
                for _p_i, _pos, _nid in _sites:
                    _v = node_id_to_consensus.get(_nid)
                    if _v is None:
                        _a1 = None
                        break
                    if _a1 is None:
                        _a1 = _v
                    elif abs(_v - _a1) > _DEC_ALT_M:
                        # Genuine wall twins (different levels): a
                        # deliberate vertical feature — keep.
                        _a1 = None
                        break
                if _a1 is None:
                    continue
                _ok = True
                _chord = None
                for _p_i, _pos, _nid in _sites:
                    _open = pending[_p_i][2][:-1]
                    _m = len(_open)
                    if _m <= 3:
                        _ok = False
                        break
                    _npr = _open[(_pos - 1) % _m]
                    _nnx = _open[(_pos + 1) % _m]
                    # ALL referencing ways must agree on the chord the
                    # removal leaves behind (the same neighbour
                    # COORDINATES): ways sharing the vertex with
                    # different neighbours (partial chain overlap)
                    # would diverge into two chords — the lens class.
                    _cend = frozenset((node_id_to_ll[_npr],
                                       node_id_to_ll[_nnx]))
                    if _chord is None:
                        _chord = _cend
                    elif _cend != _chord:
                        _ok = False
                        break
                    _la0, _lo0 = node_id_to_ll[_npr]
                    _la1, _lo1 = node_id_to_ll[_nid]
                    _la2, _lo2 = node_id_to_ll[_nnx]
                    _ax, _ay = self.ll_to_m(_la0, _lo0)
                    _bx, _by = self.ll_to_m(_la1, _lo1)
                    _cx, _cy = self.ll_to_m(_la2, _lo2)
                    _dx, _dy = _cx - _ax, _cy - _ay
                    _L2 = _dx * _dx + _dy * _dy
                    if _L2 < 1e-9 or _L2 > _DEC_MAX_CHORD_M ** 2:
                        _ok = False
                        break
                    _t = ((_bx - _ax) * _dx + (_by - _ay) * _dy) / _L2
                    if _t <= 0.0 or _t >= 1.0:
                        _ok = False
                        break
                    _perp = abs((_bx - _ax) * _dy
                                - (_by - _ay) * _dx) / math.sqrt(_L2)
                    if _perp > _DEC_PERP_M:
                        _ok = False
                        break
                    _a0 = node_id_to_consensus.get(_npr)
                    _a2 = node_id_to_consensus.get(_nnx)
                    if _a0 is None or _a2 is None:
                        _ok = False
                        break
                    if abs(_a1 - (_a0 + (_a2 - _a0) * _t)) > _DEC_ALT_M:
                        _ok = False
                        break
                if _ok:
                    _removable_ll.add(_ll)
            _removable_ll -= _law_plate_ll
            if not _removable_ll:
                break
            # MAX-CHORD RETENTION (pavement-node rule).  The redundancy
            # scan above admits an entire fine-densified straight run
            # (each vertex is near-collinear with its IMMEDIATE
            # neighbours), but the bulk removal below drops the whole run
            # in one sweep — compounding into a chord far longer than any
            # single-vertex check saw.  Walk every referencing way and,
            # wherever consecutive KEPT vertices would sit more than
            # PAVEMENT_NODE_MAX_CHORD_M apart, retain intermediate
            # removable COORDINATES.  Retention is by coordinate, so every
            # way that references it keeps the vertex and the constrained
            # chains stay identical (a per-nid retention would be the
            # one-sided-removal lens class).
            _retain_ll: set[tuple] = set()
            for _si, _s, _enids, _sa, _sna in pending:
                _open = _enids[:-1]
                _m = len(_open)
                if _m < 3:
                    continue
                _start = None
                for _k, _nid in enumerate(_open):
                    if node_id_to_ll[_nid] not in _removable_ll:
                        _start = _k
                        break
                if _start is None:
                    # Whole ring removable — the degeneracy veto below
                    # handles it; no chord to hold.
                    continue
                _anchor = self.ll_to_m(*node_id_to_ll[_open[_start]])
                _prev_ll = None
                _prev_xy = None
                _prev_removable = False
                for _step in range(1, _m + 1):
                    _idx = (_start + _step) % _m
                    _ll2 = node_id_to_ll[_open[_idx]]
                    _xy = self.ll_to_m(_ll2[0], _ll2[1])
                    _removable_here = (_ll2 in _removable_ll
                                       and _ll2 not in _retain_ll)
                    if math.hypot(_xy[0] - _anchor[0],
                                  _xy[1] - _anchor[1]) \
                            > PAVEMENT_NODE_MAX_CHORD_M:
                        # Retain the PREVIOUS vertex to hold the chord: it
                        # is within the cap of the current anchor (every
                        # ORIGINAL step is), so both resulting sub-chords
                        # stay under the cap.
                        if _prev_removable:
                            _retain_ll.add(_prev_ll)
                            _n_chord_retained += 1
                        _anchor = _prev_xy
                    if not _removable_here:
                        _anchor = _xy
                    _prev_ll = _ll2
                    _prev_xy = _xy
                    _prev_removable = _removable_here
            _removable_ll -= _retain_ll
            if not _removable_ll:
                break
            _removable: set[int] = set()
            for _ll in _removable_ll:
                for _p_i, _pos, _nid in _occ[_ll]:
                    _removable.add(_nid)
            if not _removable:
                break
            # A ring that simultaneous removals would degenerate below
            # 3 vertices VETOES its removable nids GLOBALLY — skipping
            # only that ring's update while partners drop the nid would
            # be a one-sided removal (the lens class).
            for _retry in range(4):
                _veto: set[int] = set()
                for _si, _s, _enids, _sa, _sna in pending:
                    _open = _enids[:-1]
                    if (len([_n for _n in _open
                             if _n not in _removable]) < 3):
                        _veto.update(_n for _n in _open
                                     if _n in _removable)
                if not _veto:
                    break
                _removable -= _veto
            if not _removable:
                break
            for _p_i, (_si, _s, _enids, _sa, _sna) in enumerate(pending):
                _open = [_n for _n in _enids[:-1]
                         if _n not in _removable]
                if len(_open) >= 3 and len(_open) < len(_enids) - 1:
                    pending[_p_i] = (_si, _s, _open + [_open[0]],
                                     _sa, _sna)

        if _n_chord_retained:
            UI.vprint(1,
                f"  [pav-builder] emit decimation: retained "
                f"{_n_chord_retained} vertex(es) so no pavement chord "
                f"exceeds {PAVEMENT_NODE_MAX_CHORD_M:.0f} m "
                f"(pavement-node rule).")

        # ── Tag-writing pass ────────────────────────────────────
        # Each shape's altitude tags are derived from the consensus
        # altitudes at its own corners.  Pattern detection picks
        # the tightest tag form that preserves the per-corner
        # values: all equal → flat ``altitude``; 4-corner rect
        # matching [H, L, L, H] → ``altitude_high/low``; otherwise
        # ``node_altitudes``.
        _CANON_EQ_TOL = 0.05  # 5 cm — pattern-fit tolerance
        # Rect-role shapes (taxi/runway/boundary) are planar by design;
        # collapse their [H,L,L,H] axis-end pairs to a clean rect when BOTH
        # the high-end pair (corners 0&3) and the low-end pair (corners 1&2)
        # are within this tolerance (covers solver/consensus drift).  Per
        # user 2026-05-23: 0.5 m — keep near-planar rects as rects (more
        # rects) rather than demoting them to node_altitudes.
        _RECT_COLLAPSE_TOL_M = 0.5
        _RECT_PLANAR_ROLES = (
            ROLE_BOUNDARY, ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
            ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_RUNWAY,
            ROLE_RUNWAY_CROSSING)

        # Per-node altitude: nids of compound sloping shapes whose per-corner
        # altitudes are carried as per-NODE ``alt_abs`` tags (which stock
        # Ortho4XP reads), in place of a fork-only single-way tag.
        node_alt_abs_nids: set = set()

        for s_idx, s, ext_nids, shape_altitude, shape_node_altitudes \
                in pending:
            tags = {
                "aeroway": AEROWAY_FOR_ROLE.get(s.role, "taxiway"),
                "role": s.role,
                # Stable identifier = index in ``layout.shapes`` (the
                # same ``#N`` numbering used in test-failure messages
                # and debugging).  OSM way IDs are reassigned per-file
                # by JOSM, so this tag is the reliable cross-file
                # handle for locating a specific shape.
                "shapeID": str(s_idx),
            }
            if s.ref:
                tags["ref"] = s.ref
            # Runway DE-SEGMENTATION marker (O4_RUNWAY_SINGLE_POLY): a
            # de-segmented runway is ONE ring per ref whose FAA profile
            # stations are interior long-edge vertices.  The grade TEST
            # (``check_grade``) scopes such a ring's within-shape pair
            # domain to LATERAL + same/adjacent-station (user ruling
            # 2026-07-08, ``grade_law.runway_within_pair_in_domain``); a
            # segmented rect carries no marker and keeps its full all-pair
            # check.  Emitted only for the de-seg form → gate-off builds
            # stay byte-identical.
            if getattr(s, "from_single_poly", False):
                tags["o4_single_poly"] = "1"
            # APRON-EDGE GRADE ADOPTION (USER RULING 2026-07-06): a
            # service road/junction sharing an apron edge follows the
            # apron grading rules — stamp the law override so the
            # validator applies the same cap the solver used.
            if getattr(s, "adopts_apron_grade", False):
                tags["o4_grade_law"] = "apron"
            # TAXIWAY-EDGE GRADE ADOPTION (USER RULING 2026-07-07): a
            # service-road portion inside or alongside a taxiway follows
            # the taxiway (1.5 %, letter-aware) grade law.  Stamp the law
            # override + the adjacent taxiway's code letter so the
            # validator applies the same cap the solver used.  Apron (1 %)
            # is more limiting, so it wins if both flags were set.
            elif getattr(s, "adopts_taxi_grade", False):
                tags["o4_grade_law"] = "taxi"
                _adopted_let = getattr(s, "adopted_taxi_letter", None)
                if _adopted_let:
                    tags["code_letter"] = str(_adopted_let)
            # Size-dependent taxiway grade cap (gate TAXI_GRADE_BY_WIDTH):
            # stamp the ICAO code letter so the grade validator can apply
            # the same width-dependent cap the solver used (A/B → 3 %,
            # C–F → 1.5 %).  Emitted only for sized taxiway roles when the
            # gate is on (the resolver returns None otherwise) → gate-off
            # builds carry no extra tag and stay byte-identical.
            _code_letter = taxi_shape_code_letter(self, s)
            if _code_letter:
                tags["code_letter"] = _code_letter
            # Closed ring includes the duplicate closing nid; per-
            # corner consensus altitudes follow the same indexing.
            corner_elevs = [_corner_alt(nid) for nid in ext_nids]
            n_open = max(0, len(ext_nids) - 1)
            have_all = (n_open >= 3
                        and all(e is not None
                                for e in corner_elevs[:n_open]))
            if have_all:
                # have_all guarantees the open portion is None-free;
                # the filter is a no-op that lets the checker narrow
                # ``open_alts`` to list[float].
                open_alts: list[float] = [
                    e for e in corner_elevs[:n_open] if e is not None]
                all_min = min(open_alts)
                all_max = max(open_alts)
                # Boundary STRIP rects are planar by construction:
                # flat across the strip width, sloped (or flat) only
                # along the perimeter.  Per-node consensus can pull
                # the two ends of a cross-edge to slightly different
                # altitudes where the wide strip's offset corners
                # merge (within SHARED_VERTEX_TOL_M) with a corner at
                # a neighbouring perimeter position — tight bends and
                # concave lobes, e.g. CYXY.  That tilts the quad out
                # of plane and demotes it to node_altitudes, the
                # "jagged boundary slope" artifact.  Collapse each
                # cross-edge corner pair (0&3 at one perimeter
                # vertex, 1&2 at the other) to its mean to restore
                # the flat cross-edges while keeping the consensus-
                # informed along-perimeter profile.  This covers both
                # sloped strips (altitude_high/low) AND flat-emitted
                # strips (altitude) — the latter get tilted too.
                # Bridges (node_altitudes) are genuinely non-planar
                # and are excluded.
                # Any FLAT shape stays a single flat plane (user
                # 2026-05-23): "terminals — and any flat shape — use
                # altitude= with a single value; node_altitudes is only
                # for compound sloping polygons."  If the layout settled
                # this shape on one floor altitude, per-corner consensus
                # must NOT tilt it: a drifting welded-neighbour corner
                # would otherwise average the plane out of flat and
                # demote it to a sloped node_altitudes surface (the SPJC
                # "terminal not flat" bug).  Emit the solver's flat floor;
                # welded neighbours share those nodes 1:1 and match it by
                # construction.  ROLE_BOUNDARY is excluded — strip rects
                # have their own planar (cross-edge-collapse) handling
                # just below.
                if (s.role != ROLE_BOUNDARY
                        and shape_node_altitudes is None
                        and shape_altitude is not None):
                    tags["altitude"] = f"{float(shape_altitude):.2f}"
                else:
                    # HI/LO EMISSION RETIRED (user 2026-07-06): every
                    # sloped shape ships PER-NODE altitudes — exact,
                    # human-editable, and rendering-identical for planar
                    # quads (cell_size cross-cuts only re-interpolated
                    # the plane two triangles already define).  The
                    # near-planar rect-role VALUE COLLAPSE survives as
                    # smoothing: cm-drifted [H,L,L,H] cross-edge pairs
                    # (consensus / solver noise) still collapse to their
                    # means so strip chains keep flat cross-edges — the
                    # 'jagged boundary slope' artifact fix — but the
                    # collapsed values now emit per-node like everything
                    # else.
                    if (n_open == 4
                            and s.role in _RECT_PLANAR_ROLES
                            and abs(open_alts[0] - open_alts[3])
                                    <= _RECT_COLLAPSE_TOL_M
                            and abs(open_alts[1] - open_alts[2])
                                    <= _RECT_COLLAPSE_TOL_M):
                        high_mean = (open_alts[0] + open_alts[3]) / 2.0
                        low_mean = (open_alts[1] + open_alts[2]) / 2.0
                        open_alts = [high_mean, low_mean,
                                     low_mean, high_mean]
                        for k, nid in enumerate(ext_nids[:-1]):
                            node_id_to_consensus[nid] = open_alts[k]
                    all_max = max(open_alts)
                    all_min = min(open_alts)
                    # Object-bridge terrain plates NEVER collapse to a
                    # way-level ``altitude`` tag (round 8, measured):
                    # across three fresh KBNA meshes the flat-way branch
                    # of the mesh consumer demonstrably did not land —
                    # the trench/causeway rings reached Data.node/.poly
                    # correctly (74/74 constrained segments, INTERP_ALT
                    # seeds inside) yet the built mesh kept raw DEM z at
                    # every ring vertex, while per-node ``alt_abs`` ways
                    # (the pinned junctions at 167.00) landed exactly.
                    # Flat-by-law plates therefore ship per-node.
                    force_per_node = s.role in (
                        ROLE_BRIDGE_TRENCH, ROLE_BRIDGE_CAUSEWAY,
                        ROLE_TUNNEL_TRENCH)
                    if (all_max - all_min <= _CANON_EQ_TOL
                            and not force_per_node):
                        tags["altitude"] = (
                            f"{sum(open_alts) / n_open:.2f}")
                    else:
                        # Sloping polygon: carry the per-corner altitudes
                        # as per-NODE ``alt_abs`` tags (read by stock /
                        # older Ortho4XP).  This way emits NO altitude
                        # way-tag; every one of its vertices is stamped
                        # with its consensus altitude in the node-writing
                        # pass below, so the upstream per-node override
                        # (include_patches, applied to every non-
                        # ``altitude_high/low`` way) fully specifies the
                        # ring.
                        node_alt_abs_nids.update(ext_nids)
            else:
                # No per-corner consensus available (no shape
                # contributed altitudes to these nodes).  Fall
                # back to the source shape's own tags.
                if shape_node_altitudes is not None:
                    # A per-vertex shape whose ring lost its claims
                    # (the misalignment warn / a fully-unvalued weld
                    # partner).  This branch was MISSING until
                    # 2026-07-18: the fall-through emitted NO altitude
                    # tags at all, so the way's vertices dropped onto
                    # raw DEM (EGGW tunnel plates).  Ship the way-level
                    # ``node_altitudes`` tag when the values still
                    # align with the final ring; else degrade to the
                    # flat mean — constrained, if no longer sloped.
                    _vals = [float(v) for v in shape_node_altitudes]
                    if len(_vals) == n_open:        # open convention
                        _vals = _vals + [_vals[0]]
                    if len(_vals) == n_open + 1:
                        tags["node_altitudes"] = ",".join(
                            f"{v:.2f}" for v in _vals)
                    elif _vals:
                        tags["altitude"] = (
                            f"{sum(_vals) / len(_vals):.2f}")
                elif (s.altitude_high is not None
                        and s.altitude_low is not None):
                    # hi/lo emission RETIRED (user 2026-07-06): a
                    # 4-corner source rect carries its per-corner values
                    # in the way-level ``node_altitudes`` tag (the
                    # include_patches per-node form); a reshaped
                    # non-quad flattens to the mean, as before.
                    if n_open == 4:
                        corner_values = corner_alts_from_high_low(
                            float(s.altitude_high), float(s.altitude_low))
                        tags["node_altitudes"] = ",".join(
                            f"{value:.2f}" for value in
                            corner_values + [corner_values[0]])
                    else:
                        tags["altitude"] = (
                            f"{(float(s.altitude_high) + float(s.altitude_low)) / 2.0:.2f}")
                elif s.altitude is not None:
                    tags["altitude"] = f"{s.altitude:.2f}"
            way_blocks.append((next_wid[0], ext_nids, tags))
            next_wid[0] -= 1
        rel_blocks: list[tuple[int, list[tuple[int, str]],
                               dict[str, str]]] = []

        # Crown spine breaklines (user ruling 2026-07-07, see crown.py):
        # each spine emits as an OPEN way whose nodes carry ``alt_abs``
        # at the pre-crown surface level.  ``include_patches`` inserts
        # open patch ways as constrained DUMMY edges, so the mesh
        # renders the crown ridge inside the surrounding polygon.  The
        # way carries NO ``role`` tag — every OSM reader in this repo
        # (check_grade, compare_target) selects shapes by ``role`` and
        # closed-ring geometry, so the breakline is invisible to them.
        _spines = getattr(self, "crown_spines", None) or []
        if _spines:
            _next_spine_nid = (min(node_id_to_ll) - 1
                               if node_id_to_ll else -1)
            for _pts_ll, _alts in _spines:
                _snids: list[int] = []
                for (_sla, _slo), _sa in zip(_pts_ll, _alts):
                    node_id_to_ll[_next_spine_nid] = (_sla, _slo)
                    node_id_to_consensus[_next_spine_nid] = float(_sa)
                    node_alt_abs_nids.add(_next_spine_nid)
                    _snids.append(_next_spine_nid)
                    _next_spine_nid -= 1
                if len(_snids) >= 2:
                    way_blocks.append(
                        (next_wid[0], _snids,
                         {"o4_feature": "crown_spine"}))
                    next_wid[0] -= 1

        # Gap-fill drainage spines (user design 2026-07-09, open-way
        # variant): the spine floats INSIDE the gap polygon as an open
        # constrained way — no boundary landing, no polygon split, no
        # second rail (the keyhole's slit would itself be a
        # near-parallel pair).  Same mechanism as the crown spines.
        _gspines = getattr(self, "gap_spines", None) or []
        if _gspines:
            _next_gs_nid = (min(node_id_to_ll) - 1
                            if node_id_to_ll else -1)
            for _pts_ll, _alts in _gspines:
                _gnids: list[int] = []
                for (_sla, _slo), _sa in zip(_pts_ll, _alts):
                    node_id_to_ll[_next_gs_nid] = (_sla, _slo)
                    node_id_to_consensus[_next_gs_nid] = float(_sa)
                    node_alt_abs_nids.add(_next_gs_nid)
                    _gnids.append(_next_gs_nid)
                    _next_gs_nid -= 1
                if len(_gnids) >= 2:
                    way_blocks.append(
                        (next_wid[0], _gnids,
                         {"o4_feature": "gap_drainage_spine"}))
                    next_wid[0] -= 1

        # Gap INTERIOR RING breaklines (ratified design 2026-07-11,
        # gate O4_GAP_FILL_INTERIOR_RINGS — the list is absent/empty
        # gate-OFF): band-breakpoint rings floating inside a gap face,
        # same open-constrained-way mechanism as the spines above.  A
        # chain whose first lat/lon repeats at the end is CLOSED: the
        # repeat re-uses the FIRST node id (never a second coincident
        # node — a zero-length constrained edge / coincident twin is
        # exactly the Triangle4XP degenerate class).
        _grings = getattr(self, "gap_interior_rings", None) or []
        if _grings:
            _next_gr_nid = (min(node_id_to_ll) - 1
                            if node_id_to_ll else -1)
            for _pts_ll, _alts in _grings:
                _closed = (len(_pts_ll) >= 4
                           and _pts_ll[0] == _pts_ll[-1])
                _use_pts = _pts_ll[:-1] if _closed else _pts_ll
                _use_alts = _alts[:-1] if _closed else _alts
                _rnids: list[int] = []
                for (_rla, _rlo), _ra in zip(_use_pts, _use_alts):
                    node_id_to_ll[_next_gr_nid] = (_rla, _rlo)
                    node_id_to_consensus[_next_gr_nid] = float(_ra)
                    node_alt_abs_nids.add(_next_gr_nid)
                    _rnids.append(_next_gr_nid)
                    _next_gr_nid -= 1
                if _closed and len(_rnids) >= 3:
                    _rnids.append(_rnids[0])
                if len(_rnids) >= 2:
                    way_blocks.append(
                        (next_wid[0], _rnids,
                         {"o4_feature": "gap_interior_ring"}))
                    next_wid[0] -= 1

        # Determine which interned nodes are actually referenced by
        # any emitted way (via ``way_blocks`` or ``rel_blocks``
        # member ways).  Per user 2026-04-29: discarded ring builds
        # — short rings that ``_ring_to_nids`` returned None for,
        # or rings whose nodes were dedup'd out of the final ring —
        # leave orphan entries in ``node_id_to_ll``.  Emitting
        # those produces "floating nodes" next to a polygon in
        # JOSM that aren't part of any geometry.  Filter to
        # referenced nids only.
        referenced_nids: set = set()
        for _wid, _nids, _tags in way_blocks:
            referenced_nids.update(_nids)
        # rel_blocks is currently empty in this emitter but be
        # forward-compatible if multipolygons return.
        for _rid, _members, _tags in rel_blocks:
            for _mwid, _role in _members:
                # Member ways' nids — find them in way_blocks.
                for w_id, n_list, _t in way_blocks:
                    if w_id == _mwid:
                        referenced_nids.update(n_list)
                        break
        # Stamp apt.dat provenance on the <osm> root so a later build
        # can tell whether this patch is still current (the driver's
        # freshness check, ``read_patch_source``).  The path is
        # percent-encoded: keeps the attribute value free of quotes /
        # spaces regardless of where the user's scenery pack lives.
        osm_open = ("<osm version='0.6' upload='false' "
                    "generator='O4_Airport_Pavement_Builder'")
        if self.apt_dat_path:
            osm_open += (" o4_apt_dat='"
                         + urllib.parse.quote(str(self.apt_dat_path))
                         + "'")
            try:
                osm_open += (" o4_apt_dat_mtime='"
                             + f"{os.path.getmtime(self.apt_dat_path):.6f}"
                             + "'")
            except OSError:
                pass
        # Provenance block (git sha + dirty flag, active gate configuration,
        # baked-inset DEM provenance, build timestamp + ICAO) — stamped as
        # further ``<osm>`` root attributes so it perturbs no geometry, mesh
        # consumption, or the chain-divergence audit (all read only node/way
        # elements).  ON by default; ``O4_PATCH_PROVENANCE=0`` suppresses it.
        # The assembled record is cached on the layout so the driver's log line
        # renders from the SAME truth without recomputing.
        from . import provenance as _prov
        if _prov.provenance_enabled():
            record = _prov.assemble_provenance(
                self.icao, self.dem_inset_provenance)
            self._provenance_record = record
            for _k, _v in _prov.provenance_tags(record).items():
                osm_open += f" {_k}='{_v}'"
        osm_open += ">"
        lines = [
            "<?xml version='1.0' encoding='UTF-8'?>",
            osm_open,
        ]
        for nid, (lat, lon) in sorted(node_id_to_ll.items(), reverse=True):
            if nid not in referenced_nids:
                continue
            alt_abs = (node_id_to_consensus.get(nid)
                       if nid in node_alt_abs_nids else None)
            if alt_abs is None:
                lines.append(
                    f"  <node id='{nid}' action='modify' visible='true' "
                    f"lat='{lat:.11f}' lon='{lon:.11f}' />"
                )
            else:
                # Per-node absolute altitude: the backward-compatible
                # replacement for the ``node_altitudes`` way tag.
                lines.append(
                    f"  <node id='{nid}' action='modify' visible='true' "
                    f"lat='{lat:.11f}' lon='{lon:.11f}'>"
                )
                lines.append(f"    <tag k='alt_abs' v='{alt_abs:.2f}' />")
                lines.append("  </node>")
        for wid, nids, tags in way_blocks:
            lines.append(
                f"  <way id='{wid}' action='modify' visible='true'>"
            )
            for nid in nids:
                lines.append(f"    <nd ref='{nid}' />")
            for k, v in sorted(tags.items()):
                lines.append(f"    <tag k='{k}' v='{v}' />")
            lines.append("  </way>")
        for rid, members, tags in rel_blocks:
            lines.append(
                f"  <relation id='{rid}' action='modify' visible='true'>"
            )
            for mwid, role in members:
                lines.append(
                    f"    <member type='way' ref='{mwid}' role='{role}' />"
                )
            for k, v in sorted(tags.items()):
                lines.append(f"    <tag k='{k}' v='{v}' />")
            lines.append("  </relation>")
        lines.append("</osm>")
        Path(path).write_text("\n".join(lines) + "\n")
        self._write_axes_sidecar(path)

    def _write_axes_sidecar(self, path: str) -> None:
        """Write the taxi AXES + chained ROUTES next to the patch as
        ``<path>.axes.json`` — the within-shape grade law's centerline
        context (spine membership, per-letter caps, anisotropic Δs∥
        decomposition).  ``tools/check_grade.py`` auto-loads it so the
        STANDALONE check applies the SAME law the solver and the suite
        use; without it the CLI falls back to the context-free check
        and over-flags every spine/blend-relaxed pair.  The sidecar is
        invisible to Ortho4XP (the patch loader only globs
        ``*.patch.osm``).  Best-effort: a sidecar failure never fails
        an emit.

        DEBUG-ONLY (user 2026-07-02): written only when
        ``config.LOG_VERBOSITY > 0``, so production-release patch dirs
        stay clean.  Dev iteration raises the verbosity (the suite is
        unaffected — it passes axes to ``run_checks`` directly); a
        production patch checked with the CLI reverts to the
        context-free numbers."""
        from . import config as _cfg
        if getattr(_cfg, "LOG_VERBOSITY", 0) <= 0:
            return
        try:
            import json as _json
            from .verification import (taxi_axes_ll, taxi_routes_ll,
                                       taxi_axes_exact_ll,
                                       junction_mesh_edges_ll)
            _axes_exact, _routes_exact = taxi_axes_exact_ll(self)
            data = {
                # legacy per-size-split axes (older tools); entries may carry
                # a 4th element (route ordinal into "routes")
                "axes": [list(entry) for entry in taxi_axes_ll(self)],
                "routes": taxi_routes_ll(self),
                # EXACT build_context mirror: unsplit polylines, per-SEGMENT
                # caps, route ordinal into "routes_exact" — the validator
                # reconstructs the solver's Centerline objects verbatim
                # (readers cannot drift on splitting/caps/binding).
                "axes_exact": [[pts, caps, ridx]
                               for (pts, caps, ridx) in _axes_exact],
                "routes_exact": _routes_exact,
                # The SOLVER's projection anchor: with it the validator
                # evaluates the law in the SAME meter frame the solver
                # built in (its default mean-of-nodes frame differs in
                # x-scale via cos(lat0) — millimetres over a chord,
                # enough to flip epsilon contact predicates and diverge
                # crossing verdicts between the two law readers).
                "anchor": ([self.anchor[0], self.anchor[1]]
                           if self.anchor is not None else None),
                # Tile-seam PIN vertices (user 2026-07-04): the exact
                # DEM-pinned anchors the solver graded to.  The
                # validator flags only these as seam (pin-pair pairs
                # skip, one-pin pairs check at body cap) instead of its
                # legacy 400 m blanket zone — the two readers share one
                # seam definition.
                "seam_pins": [[round(la, 7), round(lo, 7)]
                              for (la, lo) in
                              (getattr(self, "_seam_pin_ll", None) or [])],
                # Solver-declared BREAK regions (genuine anchor
                # contradictions, blended): the validator reports their
                # over-cap ramp pairs separately (user 2026-07-05).
                "break_nodes": [[round(la, 7), round(lo, 7)]
                                for (la, lo) in
                                (getattr(self, "_break_node_ll", None)
                                 or [])],
                # EXACT-MESH sidecar (user 2026-07-05): the solver's
                # junction triangle-mesh edges, consumed 1:1 by the
                # validator so emit-time ring repairs cannot mint a
                # different Delaunay than the one the solver graded to.
                "mesh_edges": junction_mesh_edges_ll(self),
                # SPINE CROWN drop field (user 2026-07-07, part 30): the
                # per-node designed crown drops the solve's writeback
                # applied.  The validator re-centres each pair's budget
                # on grade_law.crown_pair_offset from THIS field — one
                # field, both readers.
                "crown_drops": [[la, lo, c] for (la, lo, c) in
                                (getattr(self, "_crown_drop_ll", None)
                                 or [])],
                # CROWN CENTERLINE nodes (Phase 0 hotfix, user 2026-07-07):
                # the lat/lon of every centerline vertex the interior runway
                # cross-edge crown inserted at profile level.  A node on the
                # runway ridge — its grade is bounded by the SPINE PROFILE
                # (longitudinal) check + the sub-cap lateral crown by design,
                # so the validator skips within-shape runway pairs touching
                # one (a cross-station diagonal to it conflates the two).
                "crown_centerline": [[la, lo] for (la, lo) in
                                     (getattr(self, "_crown_centerline_ll",
                                              None) or [])],
                # WITHIN-SHAPE baked pair allowances (2026-07-17): the
                # exact pair selection + metre budgets the final
                # projection enforced, frozen by
                # ``final_grade_projection`` (see
                # ``verification.lockstep_pair_caps_ll``).  The
                # validator constrains exactly these pairs instead of
                # re-baking from the emitted ring — the last lockstep
                # reader (post-projection vertex inserts otherwise
                # tighten the re-baked spans below what the solver
                # lawfully enforced).
                "pair_caps": (getattr(self, "_lockstep_pair_caps_ll",
                                      None) or []),
            }
            Path(str(path) + ".axes.json").write_text(_json.dumps(data))
        except Exception:
            pass


_PATCH_SOURCE_APT_RE = re.compile(r"o4_apt_dat='([^']*)'")
_PATCH_SOURCE_MTIME_RE = re.compile(r"o4_apt_dat_mtime='([^']*)'")


def read_patch_source(path: str) -> dict | None:
    """Read the apt.dat provenance stamped into an auto-patch file.

    ``to_osm`` records the apt.dat the build consumed as
    ``o4_apt_dat`` / ``o4_apt_dat_mtime`` attributes on the ``<osm>``
    root element.  Returns ``{"apt_dat": str,
    "apt_dat_mtime": float | None}``, or ``None`` when the file is
    missing, unreadable, or pre-dates the provenance stamp.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            # The root element is line 1 or 2 (after the XML
            # declaration) — same convention O4_OSM_Utils relies on.
            line = f.readline()
            if "<osm " not in line:
                line = f.readline()
    except OSError:
        return None
    if "<osm " not in line:
        return None
    m = _PATCH_SOURCE_APT_RE.search(line)
    if not m:
        return None
    apt_dat = urllib.parse.unquote(m.group(1))
    mtime: float | None = None
    m = _PATCH_SOURCE_MTIME_RE.search(line)
    if m:
        try:
            mtime = float(m.group(1))
        except ValueError:
            mtime = None
    return {"apt_dat": apt_dat, "apt_dat_mtime": mtime}


# ──────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────
# Projection helpers
# ──────────────────────────────────────────────────────────────────

def _projection(anchor: tuple[float, float]):
    lat0, lon0 = anchor
    cos0 = math.cos(math.radians(lat0))

    @overload
    def to_m(lon: float, lat: float) -> tuple[float, float]: ...
    @overload
    def to_m(lon: float, lat: float, z: float | None
             ) -> tuple[float, float] | tuple[float, float, float]: ...

    def to_m(lon: float, lat: float, z: float | None = None
             ) -> tuple[float, float] | tuple[float, float, float]:
        x = math.radians(lon - lon0) * R_EARTH * cos0
        y = math.radians(lat - lat0) * R_EARTH
        return (x, y) if z is None else (x, y, z)

    return to_m


def _airport_anchor(apt: APR.Airport) -> tuple[float, float]:
    if apt.runways:
        r = apt.runways[0]
        return ((r.lat_a + r.lat_b) / 2.0,
                (r.lon_a + r.lon_b) / 2.0)
    if apt.boundary:
        c = apt.boundary.centroid
        return (c.y, c.x)
    return (0.0, 0.0)


# ──────────────────────────────────────────────────────────────────
# Runway rects
# ──────────────────────────────────────────────────────────────────
