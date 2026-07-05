"""Parser for X-Plane ``apt.dat`` airport data files.

Loads runway, pavement (taxiway / apron / ramp) and boundary geometry
for a single airport.  This is the *patch-mesh authoritative* source
for airport pavement shapes — apt.dat polygons are exactly what the
X-Plane simulator renders as the pavement texture, so elevation
patches generated against these polygons align perfectly with the
ground texture (no visible seams).

Compared to OSM aerodrome data:

* apt.dat polygons are **disjoint by construction** — no overlapping
  shapes to subtract, no precision-drift artefacts.
* Taxiways are stored as **outline polygons**, not centerlines that
  we have to buffer to a guessed width.
* Curved taxiway shoulders use **Bezier control points** (rows 112,
  114) which we sample into polygon vertices.
* Each pavement carries its **surface type**, **roughness** and
  **orientation** so downstream code can apply per-surface grade
  rules.

Compared to CIFP, apt.dat does NOT have per-runway-threshold
elevations — runway row 100 only stores lat/lon, width, and
displaced-threshold offsets.  CIFP is still the source of truth for
runway elevations.

The parser is read-only and side-effect-free: given a path to an
``apt.dat`` file and an ICAO code, it returns an :class:`Airport`
object containing the parsed geometry.  Use :func:`find_airport_apt_dat`
to locate the right ``apt.dat`` for a given airport, preferring a
per-airport Custom Scenery pack over the global one.
"""
from __future__ import annotations

import atexit
import math
import os
import pickle
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

# Narrow exception tuple for shapely / numeric-geometry failure
# modes.  Programming errors propagate so they surface immediately.
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────
FT_TO_M = 0.3048

# Default number of straight-line segments to sample each Bezier curve
# into.  4 produces a visibly smooth corner without exploding the
# vertex count.  Tunable via the load_airport(..., bezier_segments=N)
# parameter.  With O4_ADAPTIVE_BEZIER on (default) this is only the
# LEGACY fallback — the per-span adaptive rule below decides.
DEFAULT_BEZIER_SEGMENTS = 4

# ADAPTIVE curve tessellation (user 2026-07-05: optimise for the MINIMUM
# node count that still yields a smooth profile — short curve chords are
# the "hairline factory": a pair's grade budget is rate·distance, so a
# 1.5 m tessellation chord turns millimetres of elevation into percents
# of grade, while the vertical-curve law never lets the profile wiggle
# between 4 m nodes anyway).  Per bezier span:
#   k_sagitta = ceil(sqrt(deviation_m / CURVE_SAGITTA_MAX_M))
#       — subdivision error shrinks ~quadratically; keeps the polyline
#         within the sagitta cap of the true curve where spacing allows.
#   k_spacing = floor(span_m / CURVE_MIN_VERTEX_SPACING_M)
#       — vertices never closer than the spacing floor: 0.2·rate·d ≥ the
#         ~1 cm system noise floor at d ≥ 4-5 m (1 % class), so every
#         minted pair is robust to rounding/smoothing.
#   k = max(1, min(k_sagitta, k_spacing))   — SPACING DOMINATES (the
#         user ruling); small corner fillets collapse to 1-2 chords.
# Deterministic per span, so every shape sharing a curve derives the
# same vertices (conformance holds by construction).
ADAPTIVE_BEZIER = os.environ.get("O4_ADAPTIVE_BEZIER", "1") == "1"
CURVE_SAGITTA_MAX_M = float(os.environ.get("O4_CURVE_SAGITTA_M", "0.4"))
CURVE_MIN_VERTEX_SPACING_M = float(
    os.environ.get("O4_CURVE_MIN_SPACING_M", "4.0"))

# Per user 2026-05-04: collapse Bezier to a straight line when the
# curve's max chord deviation falls below this threshold (in degrees,
# ≈ metres at airport latitudes).  Many apt.dat authors use Beziers
# just to soften 90° corners by 1-2 m for visual smoothness — the
# default 4-segment tessellation turns each into a 5-vertex arc that
# downstream residue/junction passes treat as real boundary detail
# and end up wrapping junction polygons around.  At SPJC stub C, two
# such corner-softening Beziers (chord deviations 1.04 m and 0.39 m)
# created the "wrong-side" junction vertex.  Real curves (taxiway
# turns, swept apron edges) have deviations well above this.
# Threshold expressed in DEGREES because the calculation runs in
# lat/lon space; 0.000014 deg ≈ 1.5 m.  Tunable in METRES via
# ``O4_BEZIER_FLATTEN_DEV_M`` (default 1.5) — set 0 to keep ALL source bezier
# curves (tight corner-softening arcs included), so tight curves grade smoothly
# instead of flattening to an abrupt corner.  Trade-off: the arc vertices can
# make downstream residue/junction passes wrap a junction around the corner
# (the original reason for the flatten — user 2026-05-04).
BEZIER_FLATTEN_DEV_DEG = float(os.environ.get("O4_BEZIER_FLATTEN_DEV_M", "1.5")) / 111111.0

# Row type codes (X-Plane apt.dat 1100 / 1200 spec).
ROW_AIRPORT_HEADER = 1
ROW_RUNWAY = 100
ROW_HELIPAD = 102
ROW_PAVEMENT_HEADER = 110
ROW_NODE = 111
ROW_NODE_BEZIER = 112
ROW_CLOSE = 113
ROW_CLOSE_BEZIER = 114
ROW_END_LINE = 115             # open-polyline terminator (plain)
ROW_END_LINE_BEZIER = 116      # open-polyline terminator (bezier)
ROW_LINE_HEADER = 120          # painted linear feature (taxi lines etc.)
ROW_BOUNDARY_HEADER = 130
ROW_TAXI_NODE = 1201
ROW_TAXI_EDGE = 1202
ROW_TRUCK_EDGE = 1206          # ground-vehicle (service-road) route edge
ROW_RAMP_START = 1300          # startup location (parking position)

# Row-120 painted-line type codes that mark a TAXIWAY CENTERLINE:
# 1 = solid yellow, 7 = centerline in non-movement area; 51/57 = the
# same with a black border (apt.dat 1100 spec).  Codes 2/3/8/9 are
# taxiway EDGE / queue markings, 4/5/6 hold-position bars, 20+ roadway.
CENTERLINE_PAINT_CODES = frozenset((1, 7, 51, 57))


# ──────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────
@dataclass
class Runway:
    """One paired runway (apt.dat row 100)."""
    desig_a: str
    desig_b: str
    lat_a: float
    lon_a: float
    lat_b: float
    lon_b: float
    width_m: float
    surface_code: int
    displaced_a_m: float
    displaced_b_m: float
    blast_a_m: float = 0.0   # blast pad / overrun length beyond end a
    blast_b_m: float = 0.0   # blast pad / overrun length beyond end b
    # row-100 shoulder field, encoded ``100 * width_m + surface_code``
    # (X-Plane 12 spec): > 100 ⇒ the 100s/1000s digits are the shoulder
    # width in whole metres per side; < 100 ⇒ bare surface code (no
    # explicit width); 0 ⇒ no shoulder.
    shoulder_code: int = 0
    # Per-end runway markings code (apt.dat 1000 spec: 0 none, 1 visual,
    # 2 non-precision, 3 precision, 4 UK non-precision, 5 UK precision).
    markings_a: int = 0
    markings_b: int = 0
    # Per-end approach-lighting code (0 none, 1 ALSF-I, 2 ALSF-II,
    # 3 Calvert, 4 Calvert ILS Cat II/III, 5 SSALR, 6 SSALF, 7 SALS,
    # 8 MALSR, 9 MALSF, 10 MALS, 11 ODALS, 12 RAIL).
    approach_lights_a: int = 0
    approach_lights_b: int = 0


@dataclass
class Pavement:
    """One pavement polygon (apt.dat row 110)."""
    polygon: Polygon            # exterior + interior holes
    surface_code: int
    roughness: float            # 0.0 (smooth) … 1.0 (rough)
    orientation: float          # texture rotation in degrees from N
    name: str = ""              # pavement label, e.g. "TWY A", "RAMP 1"


@dataclass
class PaintedLine:
    """One painted linear feature (apt.dat row 120).

    ``line`` vertices are (lon, lat) like ``Pavement`` rings, bezier
    nodes already tessellated.  ``paint_codes`` collects every
    line-type attribute seen on the feature's nodes — paint types are
    < 100, lighting types ≥ 100 (the apt.dat 1100 convention).  A
    feature closed by 113/114 has ``closed=True`` (its line repeats
    the first vertex at the end); 115/116-terminated features are
    open polylines.
    """
    line: LineString
    paint_codes: frozenset[int]
    closed: bool = False

    @property
    def is_centerline_paint(self) -> bool:
        return bool(self.paint_codes & CENTERLINE_PAINT_CODES)


@dataclass
class TaxiNode:
    """One taxi-network node (apt.dat row 1201).

    Format: ``1201 lat lon usage id [label]``

    ``usage`` is one of ``"init"``, ``"dest"``, ``"both"``, or
    ``"judge"`` — describes whether the node is a route endpoint.
    Not used for centerline construction but kept for completeness.
    """
    id: int
    lat: float
    lon: float
    usage: str = ""
    label: str = ""


@dataclass
class TaxiEdge:
    """One taxi-network edge (apt.dat row 1202).

    Format: ``1202 node_from node_to direction kind [name]``

    * ``direction`` is ``"oneway"`` or ``"twoway"``.
    * ``kind`` is an ICAO width category (``"taxiway_A"`` …
      ``"taxiway_F"``) or the literal ``"runway"`` for taxi paths
      crossing a runway.
    * ``name`` is the taxiway designator (``"G"``, ``"A1"``, …) or
      the runway designator (``"02/20"``) when ``kind == "runway"``.
      May be empty for unnamed connector edges.
    """
    node_from: int
    node_to: int
    direction: str
    kind: str
    name: str = ""


@dataclass
class Airport:
    """Parsed airport geometry from one apt.dat block."""
    icao: str
    name: str
    reference_elev_ft: int      # row 1 elevation, in feet (0 if absent)
    runways: list[Runway] = field(default_factory=list)
    pavements: list[Pavement] = field(default_factory=list)
    taxi_nodes: "dict[int, TaxiNode]" = field(default_factory=dict)
    taxi_edges: list[TaxiEdge] = field(default_factory=list)
    # Ground-vehicle (service-road) route edges (row 1206).  Reuse the
    # ``TaxiEdge`` shape with ``kind == "truck"``; share the 1201 nodes
    # in ``taxi_nodes``.  Drive the 4 %-grade ``service_road`` rects.
    truck_edges: list[TaxiEdge] = field(default_factory=list)
    # Ramp-start (startup location, row 1300) positions ``(lat, lon)`` —
    # used to TRIM the last dead-end taxi-route piece leading onto a
    # stand (user 2026-07-04: the lead-in line to a parking position
    # needs no spine; the apron covers the pavement).
    ramp_starts: list = field(default_factory=list)
    # Painted linear features (row 120) — taxiway centerlines, edge
    # lines, hold bars.  Carries the authored bezier curves; airports
    # without a 1201/1202 taxi network often have ONLY these.
    painted_lines: list[PaintedLine] = field(default_factory=list)
    boundary: Polygon | None = None
    source_path: str = ""

    @property
    def reference_elev_m(self) -> float:
        return self.reference_elev_ft * FT_TO_M


@dataclass
class TaxiCenterline:
    """One taxi-route centerline, built by NETWORK CONNECTIVITY (user 2026-06-29).

    The taxi route network is grouped by *connectivity*, never by NAME: a route
    that merely changes name along its length (an unnamed apron lane continuing
    into named taxiway "F", CYXY ~U12→F) stays ONE continuous polyline, split only
    at genuine junctions (network degree ≥ 3) and runway contacts.  Grouping by
    name used to sever such routes into dangling pieces and break the spine; the
    name carries no structural meaning here — it is a label only.

    ``seg_sizes`` holds the ICAO design-code letter ("A".."F", or "" if unknown)
    for EACH segment of ``line`` (``len(seg_sizes) == len(line.coords) - 1``), read
    straight off the apt.dat row-1202 ``TaxiEdge.kind`` — so the per-segment width /
    grade cap travels on the geometry, not via a name→letter table.  A route may
    legitimately change width along its length; ``size_at_*`` resolves the letter.

    ``is_service`` marks a ground-vehicle (row-1206) route — NOT an aircraft taxi
    spine (it grades at the service-road cap and is excluded from the taxi spine),
    replacing the old ``SVC*`` name-prefix test.
    """
    line: "LineString"
    seg_sizes: list = field(default_factory=list)
    is_service: bool = False
    name: str = ""                       # label only (debug / reporting)
    # The CONTINUOUS parent route this piece was bend-split from (LOCAL-meter
    # ``LineString``).  A taxi route is split into rect-axis PIECES for the rect
    # decomposition (``split_merged_centerline``), but the grade graph needs the
    # WHOLE route's geometry to credit a climbing CURVE its full spine ARC length
    # (Δs∥): projecting a junction-body vertex onto a single short piece would
    # reset the arc at every bend (docs/anisotropic_edge_handling_plan.md §3d).
    # ``None`` ⇒ this centerline IS its own route (service road, synthetic
    # junction spine, OSM-derived) — consumers fall back to ``line``.
    route_line: "LineString | None" = None

    @property
    def chained_line(self) -> "LineString":
        """The continuous route polyline this piece belongs to (``route_line`` if
        it was bend-split from a parent, else ``line`` — it is its own route)."""
        return self.route_line if self.route_line is not None else self.line

    def _seg_index_at_arc(self, s: float) -> int:
        cs = list(self.line.coords)
        acc = 0.0
        for i in range(len(cs) - 1):
            d = math.hypot(cs[i + 1][0] - cs[i][0], cs[i + 1][1] - cs[i][1])
            if s <= acc + d + 1e-9:
                return i
            acc += d
        return max(0, len(cs) - 2)

    def size_at_arc(self, s: float) -> str:
        """ICAO size letter of the segment at arc-length ``s`` along ``line``."""
        if not self.seg_sizes:
            return ""
        i = self._seg_index_at_arc(s)
        return self.seg_sizes[i] if 0 <= i < len(self.seg_sizes) else ""

    def size_at_point(self, x: float, y: float) -> str:
        """ICAO size letter nearest point ``(x, y)`` (its projection on ``line``)."""
        try:
            from shapely.geometry import Point
            return self.size_at_arc(self.line.project(Point(x, y)))
        except Exception:                                     # pragma: no cover
            return self.seg_sizes[0] if self.seg_sizes else ""

    def dominant_size(self) -> str:
        """The widest size letter on the route (for a single-letter summary)."""
        order = "ABCDEF"
        best = ""
        for s in self.seg_sizes:
            if s in order and (best == "" or order.index(s) > order.index(best)):
                best = s
        return best


# ──────────────────────────────────────────────────────────────────────
# Public API: locating the right apt.dat
# ──────────────────────────────────────────────────────────────────────
def find_airport_apt_dat(xplane_root: str, icao: str) -> str | None:
    """Locate the most-specific ``apt.dat`` containing the given ICAO.

    Search priority:

    1. Per-airport packs in ``<X-Plane>/Custom Scenery/<pack>/Earth nav
       data/apt.dat``.  Any pack whose apt.dat starts an airport block
       for the ICAO wins.  This is what the user almost always wants:
       a custom-built scenery for that specific airport.
    2. ``<X-Plane>/Custom Scenery/Global Airports/Earth nav data/apt.dat``.
       The community-curated global file shipped with X-Plane.
    3. ``<X-Plane>/Resources/default scenery/default apt dat/Earth nav
       data/apt.dat``.  Laminar's stock fallback.

    Returns the path to the chosen apt.dat, or ``None`` if no apt.dat
    on the search path contains a header for the ICAO.

    Notes:
        * The check is "does the file contain a row 1 line whose ICAO
          field matches?"  We don't actually parse the airport — that
          would be wasteful when scanning many packs.
        * The "Global Airports" pack is itself a Custom Scenery
          directory; we explicitly defer it to step 2 so per-airport
          packs win.
    """
    if not xplane_root or not os.path.isdir(xplane_root):
        return None

    icao = icao.strip().upper()
    if not icao:
        return None

    custom_scenery = os.path.join(xplane_root, "Custom Scenery")
    # X-Plane 11 layout:
    global_pack_v11 = os.path.join(
        custom_scenery, "Global Airports", "Earth nav data", "apt.dat")
    # X-Plane 12 layout (shipped pack moved to Global Scenery):
    global_pack_v12 = os.path.join(
        xplane_root, "Global Scenery", "Global Airports",
        "Earth nav data", "apt.dat")
    default_pack = os.path.join(
        xplane_root, "Resources", "default scenery",
        "default apt dat", "Earth nav data", "apt.dat")

    # Two-pass search: prefer files that contain proper row-110
    # pavement polygons (our pipeline needs those), then fall back to
    # any file that just contains the airport.  This handles e.g. the
    # KBNA Custom Scenery pack which uses row-120 linear features but
    # no row-110 pavements — the Global pack is the right source for
    # pavement geometry there.
    custom_packs: list[str] = []
    if os.path.isdir(custom_scenery):
        for entry in sorted(os.listdir(custom_scenery)):
            if entry == "Global Airports":
                continue
            pack_apt = os.path.join(
                custom_scenery, entry, "Earth nav data", "apt.dat")
            if os.path.isfile(pack_apt) and _file_has_airport(pack_apt, icao):
                custom_packs.append(pack_apt)

    candidates: list[str] = list(custom_packs)
    for cand in (global_pack_v11, global_pack_v12):
        if os.path.isfile(cand) and _file_has_airport(cand, icao):
            candidates.append(cand)
    if os.path.isfile(default_pack) and _file_has_airport(default_pack, icao):
        candidates.append(default_pack)

    # First pass: prefer the most-specific source that ALSO has pavement.
    for cand in candidates:
        if _file_has_airport_with_pavement(cand, icao):
            return cand
    # Second pass: any file with the airport header (lets the rest of
    # the pipeline at least parse runways even if no pavements exist).
    if candidates:
        return candidates[0]
    return None


# ──────────────────────────────────────────────────────────────────────
# Public API: parsing an airport block
# ──────────────────────────────────────────────────────────────────────
def load_airport(
    aptdat_path: str,
    icao: str,
    bezier_segments: int = DEFAULT_BEZIER_SEGMENTS,
) -> Airport | None:
    """Parse the airport block for ``icao`` out of ``aptdat_path``.

    Args:
        aptdat_path: filesystem path to an apt.dat file.
        icao: 4-letter airport code (case-insensitive).
        bezier_segments: how many straight-line segments to subdivide
            each Bezier curve into.  4 is a good default for taxiway
            corners; raise it for tight curves.

    Returns:
        An :class:`Airport` object, or ``None`` if the airport block
        could not be found in the file.
    """
    if not aptdat_path or not os.path.isfile(aptdat_path):
        return None

    block = _read_airport_block(aptdat_path, icao)
    if block is None:
        return None

    header = block[0]
    # Row 1 format: ``1 elevation_ft tower_height beacon_type ICAO airport_name``
    # The name is everything after the ICAO and may contain spaces.
    parts = header.split(maxsplit=5)
    try:
        ref_elev = int(parts[1])
    except (IndexError, ValueError):
        ref_elev = 0
    name = parts[5] if len(parts) > 5 else ""

    airport = Airport(
        icao=icao.upper(),
        name=name.strip(),
        reference_elev_ft=ref_elev,
        source_path=aptdat_path,
    )

    pavement_rows: list[list[str]] = []
    boundary_rows: list[list[str]] = []
    line_rows: list[list[str]] = []
    in_pavement = False
    in_boundary = False
    in_line = False

    def flush_pavement():
        if pavement_rows:
            pav = _parse_pavement(pavement_rows, bezier_segments)
            if pav is not None:
                airport.pavements.append(pav)
            pavement_rows.clear()

    def flush_line():
        if line_rows:
            pl = _parse_painted_line(line_rows, bezier_segments)
            if pl is not None:
                airport.painted_lines.append(pl)
            line_rows.clear()

    def flush_boundary():
        if boundary_rows:
            poly = _parse_boundary(boundary_rows, bezier_segments)
            if poly is not None:
                # If we already have a boundary, union with the new one.
                if airport.boundary is None:
                    airport.boundary = poly
                else:
                    try:
                        merged = unary_union([airport.boundary, poly])
                        if isinstance(merged, Polygon):
                            airport.boundary = merged
                    except _GEOM_EXC:
                        pass
            boundary_rows.clear()

    for line in block[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        toks = stripped.split()
        try:
            row_type = int(toks[0])
        except ValueError:
            continue

        # Pavement / boundary / line blocks accumulate consecutive
        # node rows.
        if row_type == ROW_PAVEMENT_HEADER:
            flush_pavement()
            flush_boundary()
            flush_line()
            in_pavement = True
            in_boundary = False
            in_line = False
            pavement_rows.append(toks)
            continue
        if row_type == ROW_BOUNDARY_HEADER:
            flush_pavement()
            flush_boundary()
            flush_line()
            in_pavement = False
            in_boundary = True
            in_line = False
            boundary_rows.append(toks)
            continue
        if row_type == ROW_LINE_HEADER:
            flush_pavement()
            flush_boundary()
            flush_line()
            in_pavement = False
            in_boundary = False
            in_line = True
            line_rows.append(toks)
            continue
        if row_type in (ROW_NODE, ROW_NODE_BEZIER,
                        ROW_CLOSE, ROW_CLOSE_BEZIER):
            if in_pavement:
                pavement_rows.append(toks)
            elif in_boundary:
                boundary_rows.append(toks)
            elif in_line:
                line_rows.append(toks)
            continue
        if row_type in (ROW_END_LINE, ROW_END_LINE_BEZIER):
            # Open-polyline terminator: only meaningful inside a 120
            # block (115/116 appear nowhere else).
            if in_line:
                line_rows.append(toks)
                flush_line()
                in_line = False
            continue

        # Anything else terminates the current pavement / boundary /
        # line block (and contributes its own data).
        flush_pavement()
        flush_boundary()
        flush_line()
        in_pavement = False
        in_boundary = False
        in_line = False

        if row_type == ROW_RUNWAY:
            rwy = _parse_runway(toks)
            if rwy is not None:
                airport.runways.append(rwy)
        elif row_type == ROW_TAXI_NODE:
            tn = _parse_taxi_node(toks)
            if tn is not None:
                airport.taxi_nodes[tn.id] = tn
        elif row_type == ROW_TAXI_EDGE:
            te = _parse_taxi_edge(toks)
            if te is not None:
                airport.taxi_edges.append(te)
        elif row_type == ROW_RAMP_START:
            # ``1300 lat lon heading type traffic name`` — position only.
            try:
                airport.ramp_starts.append(
                    (float(toks[1]), float(toks[2])))
            except (ValueError, IndexError):
                pass
        elif row_type == ROW_TRUCK_EDGE:
            tk = _parse_truck_edge(toks)
            if tk is not None:
                airport.truck_edges.append(tk)

    # Final flush in case the block ends mid-pavement.
    flush_pavement()
    flush_boundary()
    flush_line()

    # Per user 2026-05-04: deduplicate near-identical pavement
    # polygons.  Some custom-scenery apt.dat files carry the same
    # logical pavement region drawn TWICE with slightly offset
    # vertices (e.g. SPJC's "Base Ramp" appears as both row-110
    # #39 and #40, with vertices ~0.3 m apart).  When ``unary_union``
    # later merges these duplicates, the slight offset creates
    # intersection-point artefacts on the boundary that downstream
    # residue/junction passes mistake for real apt.dat detail and
    # end up wrapping junction polygons around.  Two pavements with
    # the SAME name and a symmetric_difference / union ratio below
    # 1 % are treated as the same feature; the second one is dropped.
    if len(airport.pavements) >= 2:
        keep_idx = list(range(len(airport.pavements)))
        dropped: set = set()
        for i in range(len(airport.pavements)):
            if i in dropped:
                continue
            pi = airport.pavements[i]
            if pi.polygon is None or pi.polygon.is_empty:
                continue
            for j in range(i + 1, len(airport.pavements)):
                if j in dropped:
                    continue
                pj = airport.pavements[j]
                if pj.polygon is None or pj.polygon.is_empty:
                    continue
                if pi.name != pj.name:
                    continue
                try:
                    u = pi.polygon.union(pj.polygon)
                    if u.area <= 0:
                        continue
                    sd = pi.polygon.symmetric_difference(pj.polygon)
                    if sd.area / u.area < 0.01:
                        dropped.add(j)
                except _GEOM_EXC:
                    continue
        if dropped:
            airport.pavements = [
                p for k, p in enumerate(airport.pavements)
                if k not in dropped]

    return airport


# ──────────────────────────────────────────────────────────────────────
# Internal: file scanning
# ──────────────────────────────────────────────────────────────────────
def find_all_airport_apt_dats(xplane_root: str,
                              icao: str) -> list[str]:
    """Return EVERY apt.dat path under ``xplane_root`` that contains
    a row-1 header for ``icao`` (any pack — Custom Scenery,
    Global Airports, default).

    Different packs commonly carry different geometry for the same
    airport: a community pack might add row-110 pavement that the
    Global pack lacks, AND a custom DSF with draped polygons that
    neither has.  Callers that want the union of all available
    pavement geometry walk this list.
    """
    if not xplane_root or not os.path.isdir(xplane_root):
        return []
    icao = icao.strip().upper()
    if not icao:
        return []
    out: list[str] = []
    custom_scenery = os.path.join(xplane_root, "Custom Scenery")
    if os.path.isdir(custom_scenery):
        for entry in sorted(os.listdir(custom_scenery)):
            pack_apt = os.path.join(
                custom_scenery, entry, "Earth nav data", "apt.dat")
            if (os.path.isfile(pack_apt)
                    and _file_has_airport(pack_apt, icao)):
                out.append(pack_apt)
    global_v11 = os.path.join(
        xplane_root, "Custom Scenery", "Global Airports",
        "Earth nav data", "apt.dat")
    global_v12 = os.path.join(
        xplane_root, "Global Scenery", "Global Airports",
        "Earth nav data", "apt.dat")
    for cand in (global_v11, global_v12):
        if (os.path.isfile(cand)
                and _file_has_airport(cand, icao)
                and cand not in out):
            out.append(cand)
    default = os.path.join(
        xplane_root, "Resources", "default scenery",
        "default apt dat", "Earth nav data", "apt.dat")
    if (os.path.isfile(default)
            and _file_has_airport(default, icao)
            and default not in out):
        out.append(default)
    return out


# Process-wide cache for apt.dat header scans.  Keyed by
# (path, mtime_ns, size) so a stale entry is invalidated automatically
# if the file is rewritten during the same process run.  Populated
# lazily by :func:`_index_apt_dat` on first access; subsequent
# `_file_has_airport` / `_file_has_airport_with_pavement` calls become
# O(1) dict lookups.
#
# Why this matters: the auto-patch pipeline calls
# ``build_airport_pavement(icao, ...)`` once per airport in a tile, and
# each call invokes ``find_airport_apt_dat`` and
# ``find_all_airport_apt_dats``.  Before the cache, every airport
# invocation re-scanned every apt.dat file in the X-Plane install
# (Custom Scenery + Global + default), which on a typical setup with
# ~25 airports per tile pulled ~10 GB through the line scanner per
# tile build.  After the cache, each apt.dat is scanned exactly once
# per process.
_APT_DAT_INDEX_CACHE: dict = {}

# Persistent (cross-process) backing store for the index above.  The
# first scan of every apt.dat in an X-Plane install costs ~2.7 s (the
# Global Airports file alone is hundreds of MB); that cost is otherwise
# paid afresh by every cold process — each ``build_airport_pavement``
# run from a tool/dev loop AND every pytest-xdist worker.  Persisting the
# index to a temp file lets all of them skip the rescan after the first
# warm run.  Keys embed ``(path, mtime_ns, size)`` so a rewritten or
# moved file simply misses and is rescanned — a stale or partial cache
# file is always SAFE (mismatched keys are ignored), so loads/saves are
# wrapped to never raise.
_APT_DAT_PERSIST_PATH = os.path.join(
    tempfile.gettempdir(), "auto_patch_apt_index_v2.pkl")
_APT_DAT_PERSIST_LOADED = False
_APT_DAT_PERSIST_DIRTY = False


def _load_persistent_apt_index() -> None:
    """Merge the on-disk index into the in-memory cache (lazy, once)."""
    global _APT_DAT_PERSIST_LOADED
    if _APT_DAT_PERSIST_LOADED:
        return
    _APT_DAT_PERSIST_LOADED = True
    try:
        with open(_APT_DAT_PERSIST_PATH, "rb") as f:
            disk = pickle.load(f)
        if isinstance(disk, dict):
            for k, v in disk.items():
                _APT_DAT_INDEX_CACHE.setdefault(k, v)
    except (OSError, pickle.UnpicklingError, EOFError, ValueError,
            AttributeError):
        pass


@atexit.register
def _save_persistent_apt_index() -> None:
    """Atomically write the in-memory index to disk at process exit
    (only when new entries were added this run)."""
    if not _APT_DAT_PERSIST_DIRTY or not _APT_DAT_INDEX_CACHE:
        return
    try:
        # Merge with whatever a sibling worker may have written so we
        # don't shrink the shared cache, then atomic-rename into place.
        merged = dict(_APT_DAT_INDEX_CACHE)
        try:
            with open(_APT_DAT_PERSIST_PATH, "rb") as f:
                disk = pickle.load(f)
            if isinstance(disk, dict):
                for k, v in disk.items():
                    merged.setdefault(k, v)
        except (OSError, pickle.UnpicklingError, EOFError, ValueError,
                AttributeError):
            pass
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(_APT_DAT_PERSIST_PATH),
            prefix=".apt_index_", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                pickle.dump(merged, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, _APT_DAT_PERSIST_PATH)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        pass


def _index_apt_dat(
        aptdat_path: str) -> tuple[frozenset, frozenset, frozenset]:
    """Return ``(icaos_present, icaos_with_pavement, icaos_with_taxi)``.

    All three sets are uppercase ICAO codes.  An entry in
    ``icaos_with_pavement`` means the airport block has at least one
    row 110 (pavement header); an entry in ``icaos_with_taxi`` means it
    has at least one taxi-routing-network row (1201 node / 1202 edge),
    which is what the taxi-rect builder needs.  Result is cached
    process-wide AND in a persistent temp file (see
    :data:`_APT_DAT_PERSIST_PATH`); if the file is rewritten (mtime /
    size changes) the cache entry is invalidated and the file is
    rescanned.
    """
    global _APT_DAT_PERSIST_DIRTY
    _load_persistent_apt_index()
    try:
        st = os.stat(aptdat_path)
    except OSError:
        return frozenset(), frozenset(), frozenset()
    key = (aptdat_path, st.st_mtime_ns, st.st_size)
    cached = _APT_DAT_INDEX_CACHE.get(key)
    # Guard against a stale-format entry (e.g. a 2-tuple written by an
    # older code version that shared the persistent cache file): rescan
    # rather than unpack the wrong arity.
    if cached is not None and len(cached) == 3:
        return cached
    # Drop any stale entry for this path (different mtime/size).
    for k in [k for k in _APT_DAT_INDEX_CACHE if k[0] == aptdat_path]:
        _APT_DAT_INDEX_CACHE.pop(k, None)

    icaos = set()
    with_pavement = set()
    with_taxi = set()
    current: str | None = None
    saw_pavement_in_current = False
    saw_taxi_in_current = False
    try:
        with open(aptdat_path, "r", encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                stripped = line.lstrip()
                if stripped.startswith("1 ") or stripped.startswith("1\t"):
                    parts = stripped.split()
                    if len(parts) >= 5 and parts[0] == "1":
                        # Close out the previous airport block.
                        if current is not None and saw_pavement_in_current:
                            with_pavement.add(current)
                        if current is not None and saw_taxi_in_current:
                            with_taxi.add(current)
                        current = parts[4].upper()
                        saw_pavement_in_current = False
                        saw_taxi_in_current = False
                        icaos.add(current)
                        continue
                if current is None:
                    continue
                if (not saw_pavement_in_current
                        and (stripped.startswith("110 ")
                             or stripped.startswith("110\t"))):
                    saw_pavement_in_current = True
                elif (not saw_taxi_in_current
                        and (stripped.startswith("1201 ")
                             or stripped.startswith("1201\t")
                             or stripped.startswith("1202 ")
                             or stripped.startswith("1202\t"))):
                    saw_taxi_in_current = True
        # Close out the last block at EOF.
        if current is not None and saw_pavement_in_current:
            with_pavement.add(current)
        if current is not None and saw_taxi_in_current:
            with_taxi.add(current)
    except OSError:
        # Cache an empty result so we don't re-attempt every call.
        result = (frozenset(), frozenset(), frozenset())
        _APT_DAT_INDEX_CACHE[key] = result
        _APT_DAT_PERSIST_DIRTY = True
        return result

    result = (frozenset(icaos), frozenset(with_pavement),
              frozenset(with_taxi))
    _APT_DAT_INDEX_CACHE[key] = result
    _APT_DAT_PERSIST_DIRTY = True
    return result


def _file_has_airport(aptdat_path: str, icao: str) -> bool:
    """Return True if `aptdat_path` contains a row 1 header for ICAO.

    Backed by :func:`_index_apt_dat`'s process-wide cache; the file is
    fully scanned at most once per (path, mtime, size).
    """
    icaos, _, _ = _index_apt_dat(aptdat_path)
    return icao.upper() in icaos


def _file_has_airport_with_pavement(aptdat_path: str, icao: str) -> bool:
    """Return True if `aptdat_path` contains a row 1 header for ICAO
    AND the airport block has at least one row 110 (pavement header).

    Some Custom Scenery packs (e.g. KBNA) replace pavement polygons
    with linear-feature markup (row 120 + 111 nodes), leaving the
    airport block with 0 row-110 records.  Our pavement pipeline
    needs row-110 polygons to compute the residue/junction set, so
    such packs are unusable for pavement geometry — we fall back to
    the Global apt.dat which does have proper row-110 pavements.

    Backed by :func:`_index_apt_dat`'s process-wide cache.
    """
    _, with_pavement, _ = _index_apt_dat(aptdat_path)
    return icao.upper() in with_pavement


def _file_has_airport_with_taxi_routing(aptdat_path: str, icao: str) -> bool:
    """Return True if `aptdat_path` contains a row 1 header for ICAO
    AND the airport block has at least one taxi-routing-network row
    (1201 node / 1202 edge).

    Some Custom Scenery packs (e.g. MKStudios LPPT) draw the airport as
    draped row-110 pavement polygons + row-120 painted lines but ship NO
    1201/1202 taxi-routing graph, so our taxi-rect builder emits nothing
    and the patch comes out boundary-only.  The selector uses this to
    fall back to a candidate (Global) that does carry the network.

    Backed by :func:`_index_apt_dat`'s process-wide cache.
    """
    _, _, with_taxi = _index_apt_dat(aptdat_path)
    return icao.upper() in with_taxi


def _read_airport_block(aptdat_path: str, icao: str) -> list[str] | None:
    """Return all lines from the row-1 header for `icao` up to (but
    not including) the next row-1 header.  None if not found.
    """
    icao = icao.upper()
    block: list[str] = []
    in_block = False
    try:
        with open(aptdat_path, "r", encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                if line.startswith("1 ") or line.startswith("1\t"):
                    parts = line.split()
                    if len(parts) >= 5 and parts[0] == "1":
                        if in_block:
                            # Reached the next airport.
                            return block
                        if parts[4].upper() == icao:
                            in_block = True
                            block.append(line)
                            continue
                if in_block:
                    block.append(line)
    except OSError:
        return None
    return block if in_block else None


# ──────────────────────────────────────────────────────────────────────
# Internal: row parsers
# ──────────────────────────────────────────────────────────────────────
def _parse_runway(toks: list[str]) -> Runway | None:
    """Parse an apt.dat row 100 into a Runway.  Format:

    ``100 width surface shoulder smoothness centerline edge_lights distance_signs
         <end_a:9> <end_b:9>``

    Each end-of-runway block is 9 tokens:
    ``desig lat lon displaced blastpad markings approach_lights tdz_lights reil``

    ``blastpad`` (index 4 within the end block) is the length in
    metres of the blast pad / stopway / overrun surface beyond the
    threshold on that end.
    """
    if len(toks) < 25:
        return None
    try:
        width_m = float(toks[1])
        surface_code = int(toks[2])
        # toks[3] = shoulder, toks[4] = smoothness, toks[5] = centerline,
        # toks[6] = edge_lights, toks[7] = distance_signs
        try:
            shoulder_code = int(float(toks[3]))
        except (ValueError, IndexError):
            shoulder_code = 0
        end_a = toks[8:17]   # 9 fields
        end_b = toks[17:26]
        desig_a = end_a[0]
        lat_a = float(end_a[1])
        lon_a = float(end_a[2])
        displaced_a_m = float(end_a[3])
        blast_a_m = float(end_a[4])
        desig_b = end_b[0]
        lat_b = float(end_b[1])
        lon_b = float(end_b[2])
        displaced_b_m = float(end_b[3])
        blast_b_m = float(end_b[4])

        def _end_code(end_block: list[str], index: int) -> int:
            """Optional trailing end-block field (markings / approach
            lights); rows trimmed short of the full 9 tokens read 0."""
            try:
                return int(float(end_block[index]))
            except (ValueError, IndexError):
                return 0

        markings_a = _end_code(end_a, 5)
        approach_lights_a = _end_code(end_a, 6)
        markings_b = _end_code(end_b, 5)
        approach_lights_b = _end_code(end_b, 6)
    except (ValueError, IndexError):
        return None

    return Runway(
        desig_a=desig_a, desig_b=desig_b,
        lat_a=lat_a, lon_a=lon_a, lat_b=lat_b, lon_b=lon_b,
        width_m=width_m, surface_code=surface_code,
        displaced_a_m=displaced_a_m, displaced_b_m=displaced_b_m,
        blast_a_m=blast_a_m, blast_b_m=blast_b_m,
        shoulder_code=shoulder_code,
        markings_a=markings_a, markings_b=markings_b,
        approach_lights_a=approach_lights_a,
        approach_lights_b=approach_lights_b,
    )


def _parse_taxi_node(toks: list[str]) -> TaxiNode | None:
    """Parse an apt.dat row 1201 into a TaxiNode.

    Format: ``1201 lat lon usage id [label]``

    The label can contain spaces (e.g. ``"Props fuel truck_stop"``) —
    join all remaining tokens for it.
    """
    if len(toks) < 5:
        return None
    try:
        lat = float(toks[1])
        lon = float(toks[2])
        usage = toks[3]
        nid = int(toks[4])
    except (ValueError, IndexError):
        return None
    label = " ".join(toks[5:]) if len(toks) > 5 else ""
    return TaxiNode(id=nid, lat=lat, lon=lon, usage=usage, label=label)


def _parse_taxi_edge(toks: list[str]) -> TaxiEdge | None:
    """Parse an apt.dat row 1202 into a TaxiEdge.

    Format: ``1202 node_from node_to direction kind [name]``

    The taxiway/runway name field may be empty (the unnamed-connector
    case at CYXY — 9 of 65 edges).  The name can also contain
    spaces; join remaining tokens.
    """
    if len(toks) < 5:
        return None
    try:
        nf = int(toks[1])
        nt = int(toks[2])
    except (ValueError, IndexError):
        return None
    direction = toks[3]
    kind = toks[4]
    name = " ".join(toks[5:]) if len(toks) > 5 else ""
    return TaxiEdge(node_from=nf, node_to=nt,
                    direction=direction, kind=kind, name=name)


def _parse_truck_edge(toks: list[str]) -> TaxiEdge | None:
    """Parse an apt.dat row 1206 (ground-vehicle route edge) into a TaxiEdge.

    Format: ``1206 node_from node_to direction [name]``

    Unlike row 1202, there is no ICAO width ``kind`` field (service
    vehicles have no aircraft size class) — store ``kind == "truck"``.
    The name may be empty or contain spaces (``"Terminal fuel truck"``).
    Nodes are shared with the 1201 taxi-network nodes.
    """
    if len(toks) < 4:
        return None
    try:
        nf = int(toks[1])
        nt = int(toks[2])
    except (ValueError, IndexError):
        return None
    direction = toks[3]
    name = " ".join(toks[4:]) if len(toks) > 4 else ""
    return TaxiEdge(node_from=nf, node_to=nt,
                    direction=direction, kind="truck", name=name)


def _parse_pavement(rows: list[list[str]],
                    bezier_segments: int) -> Pavement | None:
    """Parse a row-110 header + node rows into a Pavement.

    The first contour (terminated by 113/114) is the exterior; any
    subsequent contours within the same pavement are interior holes.
    """
    if not rows:
        return None
    header = rows[0]
    try:
        surface_code = int(header[1])
        roughness = float(header[2])
        orientation = float(header[3])
    except (IndexError, ValueError):
        return None
    name = " ".join(header[4:]) if len(header) > 4 else ""

    contours = _split_contours(rows[1:])
    if not contours:
        return None

    rings = []
    for contour in contours:
        ring = _interpolate_contour(contour, bezier_segments)
        if ring:
            ring = _sparsify_ring_points(ring, True, ring[0][1])
        if len(ring) >= 3:
            # Close the ring explicitly.
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            rings.append(ring)
    if not rings:
        return None

    try:
        polygon = Polygon(rings[0], rings[1:] if len(rings) > 1 else None)
    except _GEOM_EXC:
        return None
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty:
        return None
    # buffer(0) on a self-intersecting source polygon can return a
    # MultiPolygon (the cleaned result split into disjoint pieces).
    # Take the largest component — at SPJC's custom apt.dat about 8
    # of 51 pavements take this path; the dropped slivers are tiny
    # geometric artefacts of the source data, not real pavement.
    if not isinstance(polygon, Polygon):
        if hasattr(polygon, "geoms"):
            try:
                polygon = max(polygon.geoms, key=lambda g: g.area)
            except _GEOM_EXC:
                return None
        else:
            return None
        if polygon.is_empty or not isinstance(polygon, Polygon):
            return None

    return Pavement(
        polygon=polygon,
        surface_code=surface_code,
        roughness=roughness,
        orientation=orientation,
        name=name.strip(),
    )


def _parse_boundary(rows: list[list[str]],
                    bezier_segments: int) -> Polygon | None:
    """Parse a row-130 header + node rows into a boundary Polygon.

    Boundaries follow the same node row format as pavements.  We
    treat the first contour as the exterior and any extra contours
    as interior holes (rare for boundaries but allowed by the spec).
    """
    if not rows:
        return None
    contours = _split_contours(rows[1:])
    if not contours:
        return None
    rings = []
    for contour in contours:
        ring = _interpolate_contour(contour, bezier_segments)
        if ring:
            ring = _sparsify_ring_points(ring, True, ring[0][1])
        if len(ring) >= 3:
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            rings.append(ring)
    if not rings:
        return None
    try:
        poly = Polygon(rings[0], rings[1:] if len(rings) > 1 else None)
    except _GEOM_EXC:
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or not isinstance(poly, Polygon):
        return None
    return poly


def _parse_painted_line(rows: list[list[str]],
                        bezier_segments: int) -> "PaintedLine | None":
    """Parse a row-120 header + node rows into a :class:`PaintedLine`.

    A 120 block is one polyline: 111/112 nodes terminated either by a
    113/114 row (closed loop — e.g. an apron edge outline) or a
    115/116 row (open line — the common taxiway-centerline case).
    Line-type attributes ride on the nodes (paint < 100, lighting
    ≥ 100); 115/116 terminators carry no attributes.
    """
    if len(rows) < 3:        # header + at least two nodes
        return None
    node_rows: list[list[str]] = []
    codes: set[int] = set()
    closed = False
    for row in rows[1:]:
        try:
            rt = int(row[0])
        except (ValueError, IndexError):
            continue
        if rt in (ROW_NODE, ROW_CLOSE):
            attr_start = 3
        elif rt in (ROW_NODE_BEZIER, ROW_CLOSE_BEZIER):
            attr_start = 5
        elif rt in (ROW_END_LINE, ROW_END_LINE_BEZIER):
            attr_start = None
        else:
            continue
        if rt in (ROW_CLOSE, ROW_CLOSE_BEZIER):
            closed = True
        if attr_start is not None:
            for tok in row[attr_start:attr_start + 2]:
                try:
                    codes.add(int(tok))
                except ValueError:
                    pass
        node_rows.append(row)
    if len(node_rows) < 2:
        return None
    if closed:
        pts = _interpolate_contour(node_rows, bezier_segments)
        if pts:
            pts = _sparsify_ring_points(pts, True, pts[0][1])
        if len(pts) >= 3 and pts[0] != pts[-1]:
            pts.append(pts[0])
    else:
        pts = _interpolate_open_polyline(node_rows, bezier_segments)
        if pts:
            pts = _sparsify_ring_points(pts, False, pts[0][1])
    if len(pts) < 2:
        return None
    try:
        ls = LineString(pts)
        if ls.is_empty or ls.length <= 0.0:
            return None
    except _GEOM_EXC:
        return None
    return PaintedLine(line=ls, paint_codes=frozenset(codes),
                       closed=closed)


def _interpolate_open_polyline(
        node_rows: list[list[str]],
        bezier_segments: int) -> list[tuple[float, float]]:
    """Open-polyline variant of :func:`_interpolate_contour`: same
    per-segment bezier conventions, no wraparound segment, and the
    final node IS appended."""
    n = len(node_rows)
    if n < 2:
        return []
    out: list[tuple[float, float]] = []
    for i in range(n - 1):
        a_row = node_rows[i]
        b_row = node_rows[i + 1]
        a_xy = _node_xy(a_row)
        b_xy = _node_xy(b_row)
        a_ctrl = _node_ctrl(a_row)
        b_ctrl = _node_ctrl(b_row)
        if not out or out[-1] != a_xy:
            out.append(a_xy)
        if a_ctrl is None and b_ctrl is None:
            continue
        if a_ctrl is not None and b_ctrl is None:
            ctrl_eff = a_ctrl
        elif a_ctrl is None and b_ctrl is not None:
            ctrl_eff = _mirror(b_ctrl, b_xy)
        else:
            mirrored = _mirror(b_ctrl, b_xy)
            mid = (0.5 * (a_xy[0] + b_xy[0]),
                   0.5 * (a_xy[1] + b_xy[1]))
            d1 = math.hypot(a_ctrl[0] - mid[0], a_ctrl[1] - mid[1])
            d2 = math.hypot(mirrored[0] - mid[0],
                            mirrored[1] - mid[1])
            if 0.5 * max(d1, d2) < BEZIER_FLATTEN_DEV_DEG:
                continue
            for pt in _cubic_bezier(a_xy, a_ctrl, mirrored, b_xy,
                                    _effective_bezier_segments(
                                        a_xy, (a_ctrl, mirrored), b_xy,
                                        bezier_segments))[1:-1]:
                if not out or out[-1] != pt:
                    out.append(pt)
            continue
        mid = (0.5 * (a_xy[0] + b_xy[0]),
               0.5 * (a_xy[1] + b_xy[1]))
        if 0.5 * math.hypot(ctrl_eff[0] - mid[0],
                            ctrl_eff[1] - mid[1]) \
                < BEZIER_FLATTEN_DEV_DEG:
            continue
        for pt in _quadratic_bezier(a_xy, ctrl_eff, b_xy,
                                    _effective_bezier_segments(
                                        a_xy, (ctrl_eff,), b_xy,
                                        bezier_segments))[1:-1]:
            if not out or out[-1] != pt:
                out.append(pt)
    b_last = _node_xy(node_rows[-1])
    if not out or out[-1] != b_last:
        out.append(b_last)
    return out


def _split_contours(node_rows: list[list[str]]) -> list[list[list[str]]]:
    """Walk a list of 111/112/113/114 rows and split into contours.

    A contour starts at the first row after the header (or after the
    previous contour's closing row) and ends at the next 113 / 114
    closing row.  Each returned contour is a list of node rows
    INCLUDING its closing 113/114 row.
    """
    contours: list[list[list[str]]] = []
    current: list[list[str]] = []
    for row in node_rows:
        if not row:
            continue
        try:
            row_type = int(row[0])
        except ValueError:
            continue
        if row_type not in (ROW_NODE, ROW_NODE_BEZIER,
                            ROW_CLOSE, ROW_CLOSE_BEZIER):
            continue
        current.append(row)
        if row_type in (ROW_CLOSE, ROW_CLOSE_BEZIER):
            contours.append(current)
            current = []
    # Drop a trailing un-closed contour.
    return contours


# ──────────────────────────────────────────────────────────────────────
# Internal: Bezier interpolation
# ──────────────────────────────────────────────────────────────────────
def _node_xy(row: list[str]) -> tuple[float, float]:
    """Return (lon, lat) for a node row (we use lon-first internally
    so shapely Polygons get the standard (x, y) order)."""
    return (float(row[2]), float(row[1]))


def _node_ctrl(row: list[str]) -> tuple[float, float] | None:
    """Return the Bezier control point for a 112/114/116 node, or
    None for a plain 111/113/115 node.
    """
    try:
        rt = int(row[0])
    except ValueError:
        return None
    if rt not in (ROW_NODE_BEZIER, ROW_CLOSE_BEZIER,
                  ROW_END_LINE_BEZIER):
        return None
    try:
        return (float(row[4]), float(row[3]))
    except (IndexError, ValueError):
        return None


def _sparsify_ring_points(points, closed, latitude):
    """Resample a tessellated ring/polyline to the MINIMUM vertex set
    that keeps its shape within ``CURVE_SAGITTA_MAX_M`` (user 2026-07-05:
    minimum nodes for a smooth profile).  The bezier rule alone barely
    moves the node count — most apt.dat density is AUTHORED as dense
    plain-node chains (HECA hand-tessellates its curves), so the same
    sagitta bound is applied to the assembled points via Douglas-Peucker.
    Endpoints (and for a ring, the first vertex and its antipode) are
    always kept, so a closed ring stays closed and abutting-shape
    conformance derives downstream from the same resampled geometry.
    Coordinates are (lon, lat) degrees; the tolerance converts at this
    ring's latitude."""
    if not ADAPTIVE_BEZIER or len(points) < (4 if closed else 3):
        return points
    lat_scale = 111320.0
    lon_scale = 111320.0 * math.cos(math.radians(latitude))
    tolerance_m = CURVE_SAGITTA_MAX_M

    def _perpendicular_m(point, start, end):
        sx = (end[0] - start[0]) * lon_scale
        sy = (end[1] - start[1]) * lat_scale
        px = (point[0] - start[0]) * lon_scale
        py = (point[1] - start[1]) * lat_scale
        seg_len = math.hypot(sx, sy)
        if seg_len < 1e-9:
            return math.hypot(px, py)
        return abs(px * sy - py * sx) / seg_len

    def _douglas_peucker(section):
        if len(section) <= 2:
            return section
        worst_index = 0
        worst_dev = -1.0
        for k in range(1, len(section) - 1):
            dev = _perpendicular_m(section[k], section[0], section[-1])
            if dev > worst_dev:
                worst_dev = dev
                worst_index = k
        if worst_dev <= tolerance_m:
            return [section[0], section[-1]]
        left = _douglas_peucker(section[:worst_index + 1])
        right = _douglas_peucker(section[worst_index:])
        return left[:-1] + right

    try:
        if closed:
            # Split at the first vertex and its antipode so the recursion
            # has two stable anchors; both survive, keeping the ring
            # closed and the result deterministic in the source geometry.
            half = len(points) // 2
            first = _douglas_peucker(points[:half + 1])
            second = _douglas_peucker(points[half:] + points[:1])
            out = first[:-1] + second[:-1]
            return out if len(out) >= 3 else points
        return _douglas_peucker(points)
    except RecursionError:
        # A pathological ring must degrade to the dense original, never
        # kill the airport build.
        return points


def _effective_bezier_segments(a_xy, control_points, b_xy,
                               legacy_segments):
    """Per-span ADAPTIVE segment count (see the ADAPTIVE_BEZIER constant
    block), or ``legacy_segments`` with the gate off.  Coordinates are
    (lon, lat) degrees; thresholds are metres, converted at this span's
    latitude.  Deterministic in the span's control points alone, so
    every shape sharing the curve tessellates identically."""
    if not ADAPTIVE_BEZIER:
        return legacy_segments
    lat_scale = 111320.0
    lon_scale = 111320.0 * math.cos(math.radians(a_xy[1]))

    def _meters(p, q):
        return math.hypot((p[0] - q[0]) * lon_scale,
                          (p[1] - q[1]) * lat_scale)

    control_list = list(control_points)
    span_m = 0.0
    previous = a_xy
    for point in control_list + [b_xy]:
        span_m += _meters(previous, point)
        previous = point
    chord_m = _meters(a_xy, b_xy)
    if chord_m < 1e-9:
        return max(1, legacy_segments)
    # Deviation bound: the curve never strays farther from the chord
    # than its control polygon does.
    unit_x = (b_xy[0] - a_xy[0]) * lon_scale / chord_m
    unit_y = (b_xy[1] - a_xy[1]) * lat_scale / chord_m
    deviation_m = 0.0
    for (cx, cy) in control_list:
        wx = (cx - a_xy[0]) * lon_scale
        wy = (cy - a_xy[1]) * lat_scale
        perpendicular = abs(wx * unit_y - wy * unit_x)
        if perpendicular > deviation_m:
            deviation_m = perpendicular
    if deviation_m <= 1e-9:
        return 1
    segments_for_sagitta = math.ceil(math.sqrt(
        deviation_m / max(1e-6, CURVE_SAGITTA_MAX_M)))
    segments_for_spacing = int(
        span_m // max(0.5, CURVE_MIN_VERTEX_SPACING_M))
    return max(1, min(segments_for_sagitta, segments_for_spacing))


def _quadratic_bezier(p0, p1, p2, n_segments):
    """Sample a quadratic Bezier from p0 → p2 with control p1.
    Returns a list of (n_segments + 1) points starting at p0 and
    ending at p2.
    """
    pts = []
    for i in range(n_segments + 1):
        t = i / n_segments
        omt = 1.0 - t
        x = omt * omt * p0[0] + 2 * omt * t * p1[0] + t * t * p2[0]
        y = omt * omt * p0[1] + 2 * omt * t * p1[1] + t * t * p2[1]
        pts.append((x, y))
    return pts


def _cubic_bezier(p0, p1, p2, p3, n_segments):
    """Sample a cubic Bezier from p0 → p3 with controls p1, p2.
    Returns a list of (n_segments + 1) points.
    """
    pts = []
    for i in range(n_segments + 1):
        t = i / n_segments
        omt = 1.0 - t
        b0 = omt * omt * omt
        b1 = 3 * omt * omt * t
        b2 = 3 * omt * t * t
        b3 = t * t * t
        x = b0 * p0[0] + b1 * p1[0] + b2 * p2[0] + b3 * p3[0]
        y = b0 * p0[1] + b1 * p1[1] + b2 * p2[1] + b3 * p3[1]
        pts.append((x, y))
    return pts


def _mirror(point, anchor):
    """Reflect `point` through `anchor`."""
    return (2 * anchor[0] - point[0], 2 * anchor[1] - point[1])


def _interpolate_contour(contour: list[list[str]],
                         bezier_segments: int) -> list[tuple[float, float]]:
    """Convert a contour (list of 111/112 rows ending in 113/114)
    into a flat list of (x, y) polygon vertices, sampling Bezier
    curves into straight-line segments.

    Bezier convention used here (matches X-Plane apt.dat 1100 spec):

    For each consecutive pair of nodes A → B:

    * If neither carries a control point: straight line A → B.
    * If only A has a control point (a 112 followed by a 111/113):
      quadratic Bezier from A through ctrl_a to B.
    * If only B has a control point (a 111 followed by a 112/114):
      quadratic Bezier from A through (mirror of ctrl_b across B) to B.
    * If both A and B carry control points: cubic Bezier from A,
      with controls ctrl_a and (mirror of ctrl_b across B), to B.
    """
    n = len(contour)
    if n < 2:
        return []

    # Build a closed ring of nodes (the closing 113/114 brings us
    # back to the first vertex; segment "last → first" closes the
    # contour).
    ring_nodes = list(contour)

    out: list[tuple[float, float]] = []
    for i in range(n):
        a_row = ring_nodes[i]
        b_row = ring_nodes[(i + 1) % n]
        a_xy = _node_xy(a_row)
        b_xy = _node_xy(b_row)
        a_ctrl = _node_ctrl(a_row)
        b_ctrl = _node_ctrl(b_row)

        # Append A only (B will be appended by the next iteration).
        if not out or out[-1] != a_xy:
            out.append(a_xy)

        # Zero-length span between split-handle duplicates.  WED encodes a
        # SPLIT bezier handle (independent in/out direction) as a RUN of
        # same-anchor nodes — the point before the break carries the
        # INCOMING handle, the point after the OUTGOING handle.  There is
        # no curve to draw across the zero-length gap; the duplicates'
        # handles already serve the adjacent real segments.  Without this
        # skip the degenerate span tessellates into a self-intersecting
        # SPIKE, which ``buffer(0)`` then repairs into a spurious HOLE
        # (or, for larger crossings, a MultiPolygon whose smaller pieces
        # are dropped) — losing real pavement (HECA's taxiway-fillet
        # pavements parsed with a teardrop hole).  Mirrors the DSF
        # reader's ``_interpolate_dsf_ring`` handling.
        if a_xy == b_xy:
            continue

        if a_ctrl is None and b_ctrl is None:
            # Straight line — nothing to interpolate, B will be added
            # next iteration.
            continue

        # Per user 2026-05-04: skip tessellation for Beziers whose
        # max chord deviation is below ``BEZIER_FLATTEN_DEV_DEG``.
        # These are "corner-softening" Beziers (~1 m visual rounding)
        # that don't matter for X-Plane mesh purposes but cause
        # downstream residue/junction artefacts when expanded into
        # multi-vertex arcs.
        if a_ctrl is not None and b_ctrl is None:
            ctrl_eff = a_ctrl
        elif a_ctrl is None and b_ctrl is not None:
            ctrl_eff = _mirror(b_ctrl, b_xy)
        else:
            # Cubic — measure deviation as max(|ctrl1 - midpoint|,
            # |ctrl2_mirrored - midpoint|) which bounds the curve.
            mirrored = _mirror(b_ctrl, b_xy)
            mid = (0.5 * (a_xy[0] + b_xy[0]),
                   0.5 * (a_xy[1] + b_xy[1]))
            d1 = math.hypot(a_ctrl[0] - mid[0], a_ctrl[1] - mid[1])
            d2 = math.hypot(mirrored[0] - mid[0],
                            mirrored[1] - mid[1])
            cubic_dev = 0.5 * max(d1, d2)
            if cubic_dev < BEZIER_FLATTEN_DEV_DEG:
                # Treat as straight line A→B.
                continue
            curve = _cubic_bezier(a_xy, a_ctrl, mirrored, b_xy,
                                  _effective_bezier_segments(
                                      a_xy, (a_ctrl, mirrored), b_xy,
                                      bezier_segments))
            for pt in curve[1:-1]:
                if not out or out[-1] != pt:
                    out.append(pt)
            continue
        # Quadratic Bezier path: max chord deviation is at t=0.5 and
        # equals 0.5 * dist(ctrl, midpoint(a, b)).
        mid = (0.5 * (a_xy[0] + b_xy[0]),
               0.5 * (a_xy[1] + b_xy[1]))
        quad_dev = 0.5 * math.hypot(ctrl_eff[0] - mid[0],
                                      ctrl_eff[1] - mid[1])
        if quad_dev < BEZIER_FLATTEN_DEV_DEG:
            # Treat as straight line A→B.
            continue
        curve = _quadratic_bezier(
            a_xy, ctrl_eff, b_xy,
            _effective_bezier_segments(a_xy, (ctrl_eff,), b_xy,
                                       bezier_segments))
        # Drop the first point (= a_xy, already in out) and the last
        # (= b_xy, will be appended next iteration).  Append only the
        # interior curve samples.
        for pt in curve[1:-1]:
            if not out or out[-1] != pt:
                out.append(pt)
    return out


# ──────────────────────────────────────────────────────────────────────
# Aggregate helpers
# ──────────────────────────────────────────────────────────────────────
def airport_pavement_summary(airport: Airport) -> str:
    """Short multi-line summary of an Airport's parsed contents.
    Useful for diagnostic logging during integration.
    """
    lines = [
        "Airport {}: {}".format(airport.icao, airport.name),
        "  ref elev: {} ft ({:.1f} m)".format(
            airport.reference_elev_ft, airport.reference_elev_m),
        "  source:   {}".format(airport.source_path),
        "  runways:  {}".format(len(airport.runways)),
        "  pavements: {}".format(len(airport.pavements)),
        "  taxi:     {} nodes, {} edges".format(
            len(airport.taxi_nodes), len(airport.taxi_edges)),
        "  boundary: {}".format(
            "yes ({:.0f} m² in lat-lon space)".format(
                airport.boundary.area * 12_345_679_000.0)
            if airport.boundary is not None else "no"),
    ]
    return "\n".join(lines)


def taxi_junction_points(
        airport: Airport,
        to_m: Callable[[float, float], tuple[float, float]],
) -> list[tuple[float, float]]:
    """Return apt.dat taxi-network junction node positions.

    A node is a "junction" when at least one of these holds:

      * Referenced by edges with ≥ 2 distinct taxiway names (e.g.
        the node where E meets G).
      * Referenced by ≥ 3 edges of the same name (a 3-way branch
        within one taxiway — the apex of an apron loop).
      * Touches a runway-crossing edge (kind == "runway") — the
        taxi transitions onto the runway here, so rect axes
        must terminate.

    Names equal to ``""`` (unnamed connectors) are treated as the
    sentinel ``"_conn"`` so a connector + a named taxi counts as
    two distinct names.

    Result is in meter coordinates (caller-supplied ``to_m``).
    """
    from collections import defaultdict
    if not airport.taxi_nodes or not airport.taxi_edges:
        return []

    names_at_node: dict[int, set] = defaultdict(set)
    degree_per_name: dict[tuple[int, str], int] = defaultdict(int)
    runway_touch: set = set()
    for edge in airport.taxi_edges:
        if edge.kind == "runway":
            runway_touch.add(edge.node_from)
            runway_touch.add(edge.node_to)
            continue
        key = edge.name if edge.name else "_conn"
        names_at_node[edge.node_from].add(key)
        names_at_node[edge.node_to].add(key)
        degree_per_name[(edge.node_from, key)] += 1
        degree_per_name[(edge.node_to, key)] += 1

    out: list[tuple[float, float]] = []
    for nid, names in names_at_node.items():
        is_junction = (
            len(names) >= 2
            or any(degree_per_name[(nid, n)] >= 3 for n in names)
            or nid in runway_touch)
        if not is_junction:
            continue
        if nid not in airport.taxi_nodes:
            continue
        node = airport.taxi_nodes[nid]
        out.append(to_m(node.lon, node.lat))
    return out


def _is_sized_taxiway_edge(e) -> bool:
    """True iff edge ``e`` is a row-1202 taxiway edge carrying a real ICAO width
    class (``taxiway_A``…``_F``) — not a runway-typed taxi path, not a service
    truck route."""
    if not e.kind.startswith("taxiway_"):
        return False
    return e.kind.split("_")[-1].upper() in ("A", "B", "C", "D", "E", "F")


def painted_taxi_centerlines(
        airport: Airport,
        to_m: "Callable[[float, float], tuple[float, float]]",
        pavement_union_m=None,
        runway_union_m=None,
        min_len_m: float = 8.0,
        edge_zone_m: float = 1.5,
        edge_frac_max: float = 0.5,
        on_pavement_frac_min: float = 0.7,
) -> "list[TaxiCenterline]":
    """Synthesize taxi centerlines from row-120 PAINTED lines.

    Airports without a 1201/1202 taxi-route network frequently still
    carry the real taxiway centerlines as painted line features —
    authored bezier curves, far better geometry than what strip
    discovery can reconstruct.  Returns :class:`TaxiCenterline` objects
    shaped like :func:`taxi_centerlines` so every downstream consumer
    (rect builder, grade-graph spine, junction densifier) treats either
    source interchangeably — painted lines carry no apt.dat ICAO size, so
    ``seg_sizes`` is ``""`` per segment (the default taxi grade cap).
    Names are synthetic (``P1``, ``P2``, …) so provenance is recognizable.

    "Is it really a centerline" checks (user 2026-06-11 — the same
    line resource also draws taxiway EDGE lines and other markings):

      * paint code must be in :data:`CENTERLINE_PAINT_CODES`
        (1/7/51/57 — solid-yellow centerline family); hold bars (4-6)
        and edge/queue codes are excluded;
      * closed loops are excluded (a closed "centerline" is an
        outline, not a route);
      * the line must lie ON pavement (``on_pavement_frac_min`` of
        its length within ``pavement_union_m`` +1 m) — paint floats
        on pavement by definition;
      * the line must NOT hug the pavement boundary the way an edge
        line does: at most ``edge_frac_max`` of its length inside
        the ``edge_zone_m`` band along the boundary (a real
        centerline keeps about half a taxiway width of clearance,
        crossing the band only at junction mouths);
      * portions inside ``runway_union_m`` are clipped away (exit
        centerlines extend to the runway centerline — the runway
        rects own that surface), and each surviving piece must still
        be ``min_len_m`` long.
    """
    out: "list[TaxiCenterline]" = []
    if not airport.painted_lines:
        return out
    pav_buf = None
    edge_zone = None
    if pavement_union_m is not None and not pavement_union_m.is_empty:
        try:
            pav_buf = pavement_union_m.buffer(1.0)
            edge_zone = pavement_union_m.boundary.buffer(edge_zone_m)
        except _GEOM_EXC:
            pav_buf = edge_zone = None
    k = 0
    for pl in airport.painted_lines:
        if not pl.is_centerline_paint or pl.closed:
            continue
        try:
            line_m = LineString(
                [to_m(lon, lat) for lon, lat in pl.line.coords])
        except _GEOM_EXC:
            continue
        if line_m.length < min_len_m:
            continue
        g = line_m
        if runway_union_m is not None and not runway_union_m.is_empty:
            try:
                g = g.difference(runway_union_m)
            except _GEOM_EXC:
                pass
        parts = ([g] if g.geom_type == "LineString"
                 else [q for q in getattr(g, "geoms", [])
                       if q.geom_type == "LineString"])
        for part in parts:
            if part.length < min_len_m:
                continue
            if pav_buf is not None:
                try:
                    on = part.intersection(pav_buf).length
                    if on / part.length < on_pavement_frac_min:
                        continue
                    near_edge = part.intersection(edge_zone).length
                    if near_edge / part.length > edge_frac_max:
                        continue
                except _GEOM_EXC:
                    continue
            k += 1
            out.append(TaxiCenterline(
                line=part, name=f"P{k}",
                seg_sizes=[""] * max(0, len(part.coords) - 1)))
    return out


def taxi_centerlines(
        airport: Airport,
        to_m: Callable[[float, float], tuple[float, float]],
        rwy_centerlines: list[LineString] | None = None,
) -> list[TaxiCenterline]:
    """Build taxi centerlines from apt.dat 1201/1202 rows, BY CONNECTIVITY.

    Returns a list of :class:`TaxiCenterline` — each a continuous route polyline
    (in meter space) carrying its PER-SEGMENT ICAO size.

    Model (user 2026-06-29): the routing graph is grouped by **network
    connectivity**, never by NAME.  Routes are the maximal chains of sized taxi
    edges that run through degree-2 nodes, split ONLY at genuine junctions
    (network degree ≥ 3) and runway contacts.  A route that merely changes name
    along its length — an unnamed apron lane continuing into a named taxiway
    (CYXY ~U12 → F) — stays ONE continuous polyline, so the spine never sees a
    manufactured gap at the name boundary (the old per-NAME grouping severed it
    into two dangling pieces and dead-ended the spine).

    The ICAO size travels PER SEGMENT, straight off each edge's ``kind``
    (``taxiway_C`` → "C"), so width / grade-cap is a property of the geometry, not
    a name→letter table — a route may change width along its length and each
    segment keeps its own size.

    Each connectivity route is then bend-split (``split_merged_centerline``: RDP +
    curve-skip) for the rect decomposition exactly as before; the bend splits land
    on shared vertices, so the spine still connects across them — only the
    name-induced splits (which could fall on a vertexless interior point) are
    gone.  Each emitted piece re-derives its per-segment size from the parent
    route, so size is preserved through the split.
    """
    from shapely.geometry import LineString, Point
    from collections import defaultdict
    from .pavement.centerlines import split_merged_centerline

    nodes = airport.taxi_nodes
    edges = airport.taxi_edges
    if not nodes or not edges:
        return []

    def _size_of(edge) -> str:
        if edge.kind.startswith("taxiway_"):
            lt = edge.kind.split("_")[-1].upper()
            return lt if lt in ("A", "B", "C", "D", "E", "F") else ""
        return ""

    # ── Build the sized-taxi-edge graph (BY CONNECTIVITY, not name) ──
    pos: dict[int, tuple[float, float]] = {}

    def _pos(nid):
        if nid not in pos:
            n = nodes[nid]
            pos[nid] = to_m(n.lon, n.lat)
        return pos[nid]

    adj: dict[int, list] = defaultdict(list)   # node -> [(other, size, name, key)]
    deg: dict[int, int] = defaultdict(int)
    runway_contact: set[int] = set()
    for ei, edge in enumerate(edges):
        if edge.kind == "runway":
            # A runway-typed edge contributes no pavement, but every node it
            # touches is a chart-level junction (a taxiway terminating on / crossing
            # a runway) — a route ends there.
            if edge.node_from in nodes:
                runway_contact.add(edge.node_from)
            if edge.node_to in nodes:
                runway_contact.add(edge.node_to)
            continue
        if edge.node_from not in nodes or edge.node_to not in nodes:
            continue
        ax, ay = _pos(edge.node_from)
        bx, by = _pos(edge.node_to)
        if (ax - bx) ** 2 + (ay - by) ** 2 < 0.01:
            continue                               # collapsed edge
        sz = _size_of(edge)
        nm = edge.name or ""
        adj[edge.node_from].append((edge.node_to, sz, nm, ei))
        adj[edge.node_to].append((edge.node_from, sz, nm, ei))
        deg[edge.node_from] += 1
        deg[edge.node_to] += 1
    if not adj:
        return []

    def _is_break(n: int) -> bool:
        # A route ends at a real junction (degree ≠ 2) or a runway contact; a
        # degree-2 node is a pass-through, even across a name change.
        return deg[n] != 2 or n in runway_contact

    # ── Walk maximal connectivity chains into routes (node seq + per-seg size) ──
    used: set[int] = set()
    routes: list[tuple[list[int], list[str], list[str]]] = []

    def _walk(start: int, nbr) -> tuple[list[int], list[str], list[str]]:
        node_seq = [start]
        seg_sizes: list[str] = []
        names: list[str] = []
        nxt, sz, nm, key = nbr
        while key not in used:
            used.add(key)
            node_seq.append(nxt)
            seg_sizes.append(sz)
            names.append(nm)
            if _is_break(nxt):
                break
            conts = [c for c in adj[nxt] if c[3] != key and c[3] not in used]
            if len(conts) != 1:
                break                              # not a clean degree-2 pass
            nxt, sz, nm, key = conts[0]
        return node_seq, seg_sizes, names

    for b in [n for n in adj if _is_break(n)]:     # break→break chains first
        for nbr in adj[b]:
            if nbr[3] not in used:
                routes.append(_walk(b, nbr))
    for n in list(adj):                            # any leftover (pure loops)
        for nbr in adj[n]:
            if nbr[3] not in used:
                routes.append(_walk(n, nbr))

    def _piece_sizes(piece_line, parent: TaxiCenterline) -> list[str]:
        cs = list(piece_line.coords)
        return [parent.size_at_point(0.5 * (cs[i][0] + cs[i + 1][0]),
                                     0.5 * (cs[i][1] + cs[i + 1][1]))
                for i in range(len(cs) - 1)]

    # ── Trim ramp-start lead-ins (user 2026-07-04, CYUL) ─────────────────
    # The last dead-end route piece onto a parking position (its free end
    # within ``RAMP_START_TRIM_M`` of an apt.dat row-1300 startup
    # location) carries a stand lead-in line, not a taxi spine — drop the
    # whole leaf chain so the spine stays simple; the apron still covers
    # the pavement.  Only LEAF chains qualify (one end degree-1): a
    # through-route passing near a stand is untouched.
    RAMP_START_TRIM_M = 30.0
    ramp_pts = [to_m(lon, lat)
                for (lat, lon) in getattr(airport, "ramp_starts", ())]
    if ramp_pts:
        def _near_ramp_start(nid: int) -> bool:
            x, y = _pos(nid)
            return any((x - rx) ** 2 + (y - ry) ** 2
                       <= RAMP_START_TRIM_M ** 2
                       for (rx, ry) in ramp_pts)

        RAMP_LEADIN_MAX_M = 80.0    # only the LITTLE lead-ins drop
        kept_routes = []
        for r in routes:
            node_seq = r[0]
            if len(node_seq) >= 2:
                head, tail = node_seq[0], node_seq[-1]
                head_leaf = deg[head] == 1 and head not in runway_contact
                tail_leaf = deg[tail] == 1 and tail not in runway_contact
                if ((head_leaf and _near_ramp_start(head))
                        or (tail_leaf and _near_ramp_start(tail))):
                    pts = [_pos(nid) for nid in node_seq]
                    chain_m = sum(
                        math.hypot(pts[k + 1][0] - pts[k][0],
                                   pts[k + 1][1] - pts[k][1])
                        for k in range(len(pts) - 1))
                    if chain_m <= RAMP_LEADIN_MAX_M:
                        continue
            kept_routes.append(r)
        routes = kept_routes

    # ── Build a continuous route, then bend-split it for the rect decomposition ──
    out: list[TaxiCenterline] = []
    for (node_seq, seg_sizes, names) in routes:
        if len(node_seq) < 2:
            continue
        coords = [_pos(n) for n in node_seq]
        try:
            route_line = LineString(coords)
        except (ValueError, TypeError):
            continue
        if route_line.is_empty or route_line.length < 1e-6:
            continue
        label = next((nm for nm in names if nm), "")   # representative apt.dat name
        parent = TaxiCenterline(line=route_line, seg_sizes=list(seg_sizes),
                                name=label)
        for piece_line, _nm in split_merged_centerline(
                route_line, label, rwy_centerlines):
            if piece_line is None or piece_line.is_empty:
                continue
            out.append(TaxiCenterline(
                line=piece_line, seg_sizes=_piece_sizes(piece_line, parent),
                name=label, route_line=route_line))
    return out


def snap_parallel_service_runs(
        centerlines: "list[TaxiCenterline]",
        *,
        max_sep_m: float = 9.0,
        self_arc_min_m: float = 60.0,
        min_dot: float = 0.8,
        min_run_m: float = 20.0,
) -> int:
    """Collapse PARALLEL truck-route runs onto ONE shared line (user
    2026-07-04, CYXY 'Crew cars'): a two-lane service road is mapped as
    two one-way routes (or one out-and-back LOOP whose legs run side by
    side).  Each leg used to cut its own spine a few metres from the
    other — the road solved two profiles meeting at a RIDGE down the
    middle, falling off steeply at the edges.

    Where two route lines (or a loop's two legs) run within
    ``max_sep_m`` of each other and roughly parallel for ≥``min_run_m``:
    the FIRST line's run deforms to the pair's midline, then the
    SECOND's run is replaced by the exact SUBSTRING of the first — the
    two carry IDENTICAL geometry until they naturally diverge, so the
    slice cuts one edge and the spine solves one profile (a single
    spine down the middle, per the user's ruling).

    Mutates ``centerlines`` in place; returns the number of runs merged.
    """
    from shapely.geometry import LineString, Point
    from shapely.ops import substring

    def _direction_at(line, arc):
        a = line.interpolate(max(0.0, arc - 0.5))
        b = line.interpolate(min(line.length, arc + 0.5))
        dx, dy = b.x - a.x, b.y - a.y
        dn = math.hypot(dx, dy) or 1.0
        return dx / dn, dy / dn

    def _project_excluding(line, p, arc, window):
        """Nearest point on ``line`` OUTSIDE ``arc ± window`` — for a
        LOOP's vertex, plain ``project`` returns the vertex itself
        (distance 0 at its own arc), never the opposite leg."""
        best = None                      # (dist, global_arc)
        if arc - window > 1.0:
            seg = substring(line, 0.0, arc - window)
            if seg.length > 0.5:
                u = seg.project(p)
                best = (p.distance(seg.interpolate(u)), u)
        if arc + window < line.length - 1.0:
            seg = substring(line, arc + window, line.length)
            if seg.length > 0.5:
                u = seg.project(p)
                cand = (p.distance(seg.interpolate(u)),
                        arc + window + u)
                if best is None or cand[0] < best[0]:
                    best = cand
        return best

    def _twin_mask(line_a, line_b, same_line):
        """Per-vertex-of-``line_a``: (is_twin, projected_arc_on_b)."""
        coords = list(line_a.coords)
        out = []
        arc = 0.0
        for k, (x, y) in enumerate(coords):
            if k:
                px, py = coords[k - 1]
                arc += math.hypot(x - px, y - py)
            p = Point(x, y)
            if same_line:
                hit = _project_excluding(line_b, p, arc, self_arc_min_m)
                if hit is None:
                    out.append((False, 0.0))
                    continue
                dist, u = hit
            else:
                u = line_b.project(p)
                dist = p.distance(line_b.interpolate(u))
            if dist > max_sep_m:
                out.append((False, u))
                continue
            da = _direction_at(line_a, arc)
            db = _direction_at(line_b, u)
            if abs(da[0] * db[0] + da[1] * db[1]) < min_dot:
                out.append((False, u))
                continue
            # loops: only the LATER leg rewrites onto the earlier one
            if same_line and u >= arc:
                out.append((False, u))
                continue
            out.append((True, u))
        return coords, out

    def _runs(coords, mask):
        """Contiguous twin vertex runs [(k1, k2)] spanning ≥min_run_m."""
        spans = []
        k = 0
        while k < len(coords):
            if not mask[k][0]:
                k += 1
                continue
            k2 = k
            while k2 + 1 < len(coords) and mask[k2 + 1][0]:
                k2 += 1
            run_m = sum(math.hypot(coords[t + 1][0] - coords[t][0],
                                   coords[t + 1][1] - coords[t][1])
                        for t in range(k, k2))
            if k2 > k and run_m >= min_run_m:
                spans.append((k, k2))
            k = k2 + 1
        return spans

    n_merged = 0
    n = len(centerlines)
    for i in range(n):
        for j in range(i, n):
            same = i == j
            line_i = centerlines[i].line
            line_j = centerlines[j].line
            if line_i is None or line_j is None \
                    or line_i.is_empty or line_j.is_empty:
                continue
            if not same and line_i.distance(line_j) > max_sep_m:
                continue
            if not same:
                # Step A: deform line_i's twin run to the pair MIDLINE.
                coords_i, mask_i = _twin_mask(line_i, line_j, False)
                spans_i = _runs(coords_i, mask_i)
                if not spans_i:
                    continue
                new_i = list(coords_i)
                for (k1, k2) in spans_i:
                    for k in range(k1, k2 + 1):
                        q = line_j.interpolate(mask_i[k][1])
                        new_i[k] = (0.5 * (coords_i[k][0] + q.x),
                                    0.5 * (coords_i[k][1] + q.y))
                try:
                    line_i = LineString(new_i)
                except (ValueError, TypeError):
                    continue
                centerlines[i].line = line_i
            # Step B: replace line_j's twin run with the exact SUBSTRING
            # of (possibly deformed) line_i — identical geometry.
            coords_j, mask_j = _twin_mask(line_j, line_i, same)
            spans_j = _runs(coords_j, mask_j)
            if not spans_j:
                continue
            new_coords = []
            cursor = 0
            for (k1, k2) in spans_j:
                new_coords.extend(coords_j[cursor:k1])
                u1, u2 = mask_j[k1][1], mask_j[k2][1]
                try:
                    seg = substring(line_i, u1, u2)
                except (ValueError, TypeError):
                    new_coords.extend(coords_j[k1:k2 + 1])
                    cursor = k2 + 1
                    continue
                seg_coords = list(getattr(seg, "coords", ()))
                if len(seg_coords) < 2:
                    new_coords.extend(coords_j[k1:k2 + 1])
                else:
                    new_coords.extend(seg_coords)
                    n_merged += 1
                cursor = k2 + 1
            new_coords.extend(coords_j[cursor:])
            if len(new_coords) >= 2:
                try:
                    centerlines[j].line = LineString(new_coords)
                except (ValueError, TypeError):
                    pass
    return n_merged


def service_road_centerlines(
        airport: Airport,
        to_m: Callable[[float, float], tuple[float, float]],
) -> list[tuple[LineString, str]]:
    """Build ground-vehicle (service-road) centerlines from apt.dat
    1206 truck-route edges + the shared 1201 nodes.

    Returns ``[(LineString_in_meter_space, route_name)]`` — the same
    shape as :func:`taxi_centerlines`, so the rect builder can consume
    it when emitting 4 %-grade ``service_road`` rects (Phase 3).

    Construction is a simple per-name ``linemerge`` (service roads
    have no chart-level junction structure to pre-split at, unlike the
    aircraft taxi network).  Returns an empty list when there are no
    1206 edges (the common case for apt.dat blocks without a ground
    vehicle network).
    """
    from shapely.geometry import LineString, MultiLineString
    from shapely.ops import linemerge

    nodes = airport.taxi_nodes
    edges = airport.truck_edges
    if not nodes or not edges:
        return []

    by_name: dict[str, list[LineString]] = {}
    for edge in edges:
        if edge.node_from not in nodes or edge.node_to not in nodes:
            continue
        na = nodes[edge.node_from]
        nb = nodes[edge.node_to]
        ax, ay = to_m(na.lon, na.lat)
        bx, by = to_m(nb.lon, nb.lat)
        if (ax - bx) ** 2 + (ay - by) ** 2 < 0.01:
            continue
        try:
            seg = LineString([(ax, ay), (bx, by)])
        except (ValueError, TypeError):
            continue
        by_name.setdefault(edge.name, []).append(seg)

    out: list[TaxiCenterline] = []
    for name, segments in by_name.items():
        if len(segments) == 1:
            merged_lines = [segments[0]]
        else:
            try:
                merged = linemerge(MultiLineString(segments))
            except (ValueError, TypeError):
                merged_lines = list(segments)
            else:
                if merged.is_empty:
                    continue
                if merged.geom_type == "LineString":
                    merged_lines = [merged]
                else:   # MultiLineString
                    merged_lines = [ls for ls in merged.geoms
                                    if not ls.is_empty]
        for ls in merged_lines:
            if ls.length > 0:
                # Ground-vehicle route: is_service marks it OUT of the taxi spine
                # (no ICAO taxi size; graded at the service-road cap by role).
                out.append(TaxiCenterline(
                    line=ls, seg_sizes=[""] * max(0, len(ls.coords) - 1),
                    is_service=True, name=name or ""))
    return out