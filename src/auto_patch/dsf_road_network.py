"""Read X-Plane DSF vector ROAD NETWORKS from a DSFTool text dump.

A DSF may carry a vector road network alongside (or instead of) its
draped pavement.  X-Plane draws these networks by extruding a ``.net``
road definition along polylines the DSF stores as *segments* joined at
*junctions*.  The whole content of the sibling ``US-KBNA Nashville
Roads`` pack is one such network: the roads that pass beneath the KBNA
taxiway bridges live ONLY here — the airport pack's apt.dat has zero
code-110 rows for them and ``dsf_reader`` (draped ``.pol`` polygons)
never sees them.  This module is the road source for that class of
bridge, sitting beside the OpenStreetMap roads that ``bridges.py`` and
``clearance.py`` consume today.

We do not reimplement DSF binary parsing; as with :mod:`dsf_reader`, the
bundled ``DSFTool --dsf2text`` converts DSF→text losslessly and this
module parses the text.

Verified grammar (DSFTool 2.4.0-b1, ``US-KBNA Nashville Roads``
``+36-087.dsf``, 2026-07-09).  All coordinates are longitude/latitude in
EPSG:4326; the caller projects to its local metre frame::

    NETWORK_DEF   <library .net path>
    BEGIN_SEGMENT <def_index> <road_subtype> <junction_id> <lon> <lat> <level>
    SHAPE_POINT   <lon> <lat> <level>
    ...
    END_SEGMENT   <junction_id> <lon> <lat> <level>

* ``NETWORK_DEF`` builds a 0-based table of ``.net`` library paths.  A
  segment's first integer (``def_index``) indexes it.  The DSF format
  permits several definitions; the KBNA pack ships exactly one
  (``lib/g10/roads_EU.net``) and every segment uses index 0.
* ``road_subtype`` (the second BEGIN_SEGMENT integer) selects the road
  kind WITHIN the ``.net`` — the DSF spec also calls this the "road
  type".  At KBNA the Donelson Pike carriageways draped under the
  taxiway-L bridge are subtype 20; the relocated interchange ramps
  nearby are subtype 60.  Observed histogram: subtypes 10/20/24/30/31/
  34/40/43/44/50/60/70/71/220.
* The third column of BEGIN/END (per-junction) and the third column of
  SHAPE_POINT (per-shape-point) is the vertical **LEVEL FLAG**, NOT a
  metric elevation.  ``HEIGHTS 0.50000 0.0`` quantises it to half-unit
  steps but in practice it emits small integers: **0 = draped onto the
  terrain, 1+ = elevated** (a stacked bridge level).  This flag is the
  decisive signal for feature B: a road that drapes (level 0) beneath a
  taxiway bridge needs a depressed terrain corridor, whereas an elevated
  ramp (level 1+) flies over on its own structure and must be left
  alone.  DSFTool writes the ground level as ``-0.000000000``; treat any
  magnitude below :data:`LEVEL_DRAPED_MAX_ABS` as draped.  KBNA levels
  span 0..2.
* The full polyline of a segment is the BEGIN node, then every
  SHAPE_POINT in order, then the END node — see
  :attr:`RoadSegment.shape_points`, which includes all three.

Gotchas learned in the wild:

* DSFTool separates tokens with plain spaces here, but split on
  whitespace anyway (the OBJ8 exports that broke ``obj8_reader`` were
  tab-separated; be defensive by default).
* ``PROPERTY sim/exclude_net`` rows carry their own ``lon/lat`` lists in
  a completely different, semicolon/comma-packed syntax.  They are NOT
  road geometry (they are keep-out boxes) and are ignored here — parsing
  a road polyline out of one would inject phantom segments.
* A dump may hold road geometry with no ``NETWORK_DEF`` at all if the
  definition table lives in a companion DSF; a segment whose
  ``def_index`` is out of range still parses, with an empty
  ``network_definition_path`` rather than a crash.

Malformed-input policy (matching the spirit of :mod:`obj8_reader`, which
silently ignores any line it does not recognise): unrecognised lines are
skipped silently.  A *recognised* command line that cannot be parsed
(wrong token count, non-numeric coordinate, ``SHAPE_POINT`` /
``END_SEGMENT`` with no open segment, or a segment left unterminated at
the next ``BEGIN_SEGMENT`` / end of file) is skipped AND counted in
:attr:`RoadNetwork.skipped_line_count`, never raised — a reader over
third-party tool output must not abort a whole tile on one bad row.
"""

from __future__ import annotations

from typing import Iterable, NamedTuple


# The network vertical column is a draping LEVEL, quantised to half-unit
# steps by ``HEIGHTS 0.50000``.  Level 0 (emitted as ``-0.000000000``)
# means draped onto the terrain; anything of larger magnitude is an
# elevated bridge level.  Half of the quantisation step is the cleanest
# draped/elevated cut.
LEVEL_DRAPED_MAX_ABS = 0.5


class RoadShapePoint(NamedTuple):
    """One vertex of a road polyline in EPSG:4326.

    ``level`` is the vertical draping level exactly as the dump carried
    it (0 = on the terrain, 1+ = an elevated bridge level); ``draped`` is
    the decoded boolean (``abs(level) < LEVEL_DRAPED_MAX_ABS``), stored
    so consumers need not repeat the threshold.
    """

    longitude: float
    latitude: float
    level: float
    draped: bool


class RoadSegment(NamedTuple):
    """One road segment: a polyline between two junctions.

    ``network_definition_index`` indexes the dump's ``NETWORK_DEF`` table
    and ``network_definition_path`` is the resolved ``.net`` library path
    (empty when the index is out of range).  ``road_subtype`` selects the
    road kind within that ``.net``.  ``shape_points`` is the complete
    ordered polyline — the BEGIN junction node, every intermediate
    SHAPE_POINT, then the END junction node — so a consumer can build the
    line without stitching the junction endpoints back on.
    """

    network_definition_index: int
    network_definition_path: str
    road_subtype: int
    start_junction_id: int
    end_junction_id: int
    shape_points: list[RoadShapePoint]

    @property
    def is_fully_draped(self) -> bool:
        """True when every vertex is draped onto the terrain (level 0) —
        the signature of a road that passes UNDER a bridge and wants a
        depressed corridor, as opposed to one that ramps up over it."""
        return all(point.draped for point in self.shape_points)


class RoadNetwork(NamedTuple):
    """Every road segment parsed from one DSF text dump.

    ``network_definitions`` is the full ``NETWORK_DEF`` table (usually a
    single ``.net`` path); :attr:`network_definition` is the convenience
    accessor for the common single-definition case.  ``skipped_line_count``
    reports malformed recognised-command lines that were dropped.
    """

    network_definitions: list[str]
    segments: list[RoadSegment]
    skipped_line_count: int

    @property
    def network_definition(self) -> str | None:
        """The sole ``.net`` library path when the dump declares exactly
        one (the KBNA case), otherwise the first, or ``None`` when the
        table is empty.  Read ``network_definitions`` for the full table
        when a dump carries several."""
        if not self.network_definitions:
            return None
        return self.network_definitions[0]


def _to_draped(level: float) -> bool:
    return abs(level) < LEVEL_DRAPED_MAX_ABS


def parse_dsf_road_networks(dsf_text_lines: Iterable[str]) -> RoadNetwork:
    """Parse road networks from an iterable of DSFTool dump lines.

    Takes lines rather than a path so tests can feed synthetic snippets
    directly (the harness pattern used across the reader suite).  See the
    module docstring for the grammar and the malformed-input policy.
    """
    network_definitions: list[str] = []
    segments: list[RoadSegment] = []
    skipped_line_count = 0

    # State for the segment currently being accumulated.
    current_definition_index: int | None = None
    current_subtype = 0
    current_start_junction = 0
    current_points: list[RoadShapePoint] = []

    def _definition_path(index: int) -> str:
        if 0 <= index < len(network_definitions):
            return network_definitions[index]
        return ""

    for line in dsf_text_lines:
        tokens = line.split()
        if not tokens:
            continue
        keyword = tokens[0]

        if keyword == "NETWORK_DEF":
            # Everything after the keyword is the library path (a .net
            # path has no spaces in practice, but keep the remainder
            # intact rather than only tokens[1]).  A bare NETWORK_DEF
            # with no path is malformed: skip and count per the policy.
            remainder = line.split(None, 1)
            if len(remainder) < 2 or not remainder[1].strip():
                skipped_line_count += 1
                continue
            network_definitions.append(remainder[1].strip())

        elif keyword == "BEGIN_SEGMENT":
            # A new segment while one is open means the previous one was
            # never terminated — drop and count it.
            if current_definition_index is not None:
                skipped_line_count += 1
            try:
                current_definition_index = int(tokens[1])
                current_subtype = int(tokens[2])
                current_start_junction = int(tokens[3])
                longitude = float(tokens[4])
                latitude = float(tokens[5])
                level = float(tokens[6])
            except (IndexError, ValueError):
                current_definition_index = None
                current_points = []
                skipped_line_count += 1
                continue
            current_points = [
                RoadShapePoint(longitude, latitude, level, _to_draped(level))
            ]

        elif keyword == "SHAPE_POINT":
            if current_definition_index is None:
                skipped_line_count += 1
                continue
            try:
                longitude = float(tokens[1])
                latitude = float(tokens[2])
                level = float(tokens[3])
            except (IndexError, ValueError):
                skipped_line_count += 1
                continue
            current_points.append(
                RoadShapePoint(longitude, latitude, level, _to_draped(level))
            )

        elif keyword == "END_SEGMENT":
            if current_definition_index is None:
                skipped_line_count += 1
                continue
            try:
                end_junction = int(tokens[1])
                longitude = float(tokens[2])
                latitude = float(tokens[3])
                level = float(tokens[4])
            except (IndexError, ValueError):
                skipped_line_count += 1
                current_definition_index = None
                current_points = []
                continue
            current_points.append(
                RoadShapePoint(longitude, latitude, level, _to_draped(level))
            )
            segments.append(
                RoadSegment(
                    network_definition_index=current_definition_index,
                    network_definition_path=_definition_path(
                        current_definition_index
                    ),
                    road_subtype=current_subtype,
                    start_junction_id=current_start_junction,
                    end_junction_id=end_junction,
                    shape_points=current_points,
                )
            )
            current_definition_index = None
            current_points = []

    # A segment left open at end of file is malformed — count it.
    if current_definition_index is not None:
        skipped_line_count += 1

    return RoadNetwork(
        network_definitions=network_definitions,
        segments=segments,
        skipped_line_count=skipped_line_count,
    )


def read_dsf_road_networks(dsf_text_dump_path: str) -> RoadNetwork:
    """Parse road networks from a DSFTool ``--dsf2text`` dump file.

    Thin file wrapper over :func:`parse_dsf_road_networks`.  The caller
    is responsible for producing the text dump (see :mod:`dsf_reader`'s
    ``DSFTool`` invocation); this reader consumes the text so it stays
    trivially testable against synthetic snippets.
    """
    with open(dsf_text_dump_path, errors="replace") as handle:
        return parse_dsf_road_networks(handle)


def segments_crossing(
    network: RoadNetwork,
    polygon_longitude_latitude: Iterable[tuple[float, float]],
) -> list[RoadSegment]:
    """Return the segments whose polyline intersects ``polygon`` (a ring
    of ``(longitude, latitude)`` vertices), for spatially filtering a
    whole-tile network down to one bridge's footprint.

    Uses shapely (already a project dependency), imported lazily so the
    reader stays importable in contexts that never call this helper.  A
    segment of a single point is treated as that point; the polygon ring
    is closed automatically by shapely.
    """
    from shapely.geometry import LineString, Point, Polygon

    ring = list(polygon_longitude_latitude)
    footprint = Polygon(ring)
    hits: list[RoadSegment] = []
    for segment in network.segments:
        coordinates = [
            (point.longitude, point.latitude) for point in segment.shape_points
        ]
        if not coordinates:
            continue
        geometry = (
            Point(coordinates[0])
            if len(coordinates) == 1
            else LineString(coordinates)
        )
        if footprint.intersects(geometry):
            hits.append(segment)
    return hits
