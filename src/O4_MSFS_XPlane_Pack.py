"""X-Plane output side of the "Convert MSFS airport" feature.

This module produces the X-Plane Custom Scenery artifacts for a converted
MSFS airport:

  * ``find_airport_near`` / ``extract_airport_from_global_apt_dat`` /
    ``write_pack_apt_dat`` copy the default airport's ``apt.dat`` block
    out of the X-Plane Global Airports gateway into the new pack;
  * ``compute_exclusion_rectangles`` groups the converted object
    placements into degree rectangles used to suppress the default
    gateway 3D underneath them;
  * ``write_overlay_dsf`` builds the overlay DSF text and runs the
    Laminar ``DSFTool --text2dsf`` binary to place every converted
    object and write the ``sim/exclude_obj|fac|agp`` rectangles.

The Global Airports gateway files are never edited: default facades and
objects are suppressed with exclusion zones in the new pack's own
overlay DSF, the standard reversible X-Plane mechanism.

Core module: no GUI-toolkit imports. Standard library only.

Build-time impact: none - this module is part of the "Convert MSFS
airport" tool, not the per-tile scenery build pipeline, so it has no
bearing on the per-airport auto-patch or whole-tile build budgets.
"""
from __future__ import annotations

import math
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Row types whose first column marks the start of an airport block and
# whose fifth column carries the airport identifier: land airport (1),
# seaport (16), heliport (17).
_AIRPORT_HEADER_ROW_TYPES = frozenset({"1", "16", "17"})

# Column index (0-based) of the airport identifier in a header row:
# "<type> <elevation> <deprecated> <deprecated> <identifier> <name...>".
_HEADER_IDENTIFIER_COLUMN = 4

# Column indices of latitude/longitude for the two runway ends of a
# row-type 100 land runway record.
_RUNWAY_END_ONE_LATITUDE_COLUMN = 9
_RUNWAY_END_ONE_LONGITUDE_COLUMN = 10
_RUNWAY_END_TWO_LATITUDE_COLUMN = 18
_RUNWAY_END_TWO_LONGITUDE_COLUMN = 19

# Column indices of latitude/longitude for a row-type 102 helipad record:
# "102 <designator> <lat> <lon> ...".
_HELIPAD_LATITUDE_COLUMN = 2
_HELIPAD_LONGITUDE_COLUMN = 3

# Metres of latitude per degree (mean); used to convert padding distances
# expressed in metres into degree offsets.
_METRES_PER_DEGREE_LATITUDE = 111320.0

# Placements spanning at most this many kilometres may be collapsed into a
# single bounding exclusion rectangle.
_SINGLE_RECTANGLE_SPAN_KILOMETRES = 2.0


@dataclass(frozen=True)
class PlacedObject:
    """A single converted object placed at a geographic position.

    ``object_relative_path`` is the pack-relative path with forward
    slashes, for example ``"objects/foo.obj"``. ``heading_degrees_true``
    is the true heading in degrees clockwise from north.

    ``altitude_meters`` and ``is_above_ground`` carry the MSFS placement
    altitude: above-ground metres when ``is_above_ground`` is true,
    absolute MSL metres otherwise.  An altitude of exactly 0 is written
    as a plain ground-draped ``OBJECT`` row regardless of the flag (the
    overwhelmingly common case, and the safe reading of the ambiguous
    flag-false zero -- MSL 0 would sink an inland object to sea level).

    ``bounds_xz`` is the object's horizontal footprint in OBJ8 meters,
    ``(min_x, min_z, max_x, max_z)`` with +X east and +Z south at
    heading 0 (the converter manifest's ``bounds_xz``).  When present,
    exclusion rectangles cover the footprint rotated by the placement
    heading instead of just the placement point.
    """

    object_relative_path: str
    longitude: float
    latitude: float
    heading_degrees_true: float
    altitude_meters: float = 0.0
    is_above_ground: bool = True
    bounds_xz: Optional[Tuple[float, float, float, float]] = None


def _parse_header_identifier(stripped_line: str) -> Optional[str]:
    """Return the airport identifier if ``stripped_line`` is a header row.

    ``stripped_line`` must already have surrounding whitespace removed.
    Returns ``None`` for any line that is not an airport header row or
    that lacks an identifier column.
    """
    columns = stripped_line.split()
    if not columns or columns[0] not in _AIRPORT_HEADER_ROW_TYPES:
        return None
    if len(columns) <= _HEADER_IDENTIFIER_COLUMN:
        return None
    return columns[_HEADER_IDENTIFIER_COLUMN]


def _haversine_kilometres(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    """Great-circle distance in kilometres between two positions."""
    earth_radius_kilometres = 6371.0088
    latitude_a_radians = math.radians(latitude_a)
    latitude_b_radians = math.radians(latitude_b)
    delta_latitude = math.radians(latitude_b - latitude_a)
    delta_longitude = math.radians(longitude_b - longitude_a)
    haversine_term = (
        math.sin(delta_latitude / 2.0) ** 2
        + math.cos(latitude_a_radians)
        * math.cos(latitude_b_radians)
        * math.sin(delta_longitude / 2.0) ** 2
    )
    return (
        2.0
        * earth_radius_kilometres
        * math.asin(min(1.0, math.sqrt(haversine_term)))
    )


class _AirportPositionAccumulator:
    """Collects position candidates for one airport block while streaming.

    Position source priority, matching the feature specification:
    the 1302 ``datum_lat``/``datum_lon`` metadata rows, then the first
    runway (row 100) midpoint, then the first helipad (row 102).
    """

    def __init__(self, identifier: str) -> None:
        self.identifier = identifier
        self._datum_latitude: Optional[float] = None
        self._datum_longitude: Optional[float] = None
        self._runway_latitude: Optional[float] = None
        self._runway_longitude: Optional[float] = None
        self._helipad_latitude: Optional[float] = None
        self._helipad_longitude: Optional[float] = None

    def consider_row(self, columns: List[str]) -> None:
        """Update the accumulator from one whitespace-split data row."""
        if not columns:
            return
        row_type = columns[0]
        if row_type == "1302" and len(columns) >= 3:
            key = columns[1]
            if key == "datum_lat":
                self._datum_latitude = _safe_float(columns[2])
            elif key == "datum_lon":
                self._datum_longitude = _safe_float(columns[2])
        elif row_type == "100" and self._runway_latitude is None:
            end_one_latitude = _safe_float_at(
                columns, _RUNWAY_END_ONE_LATITUDE_COLUMN
            )
            end_one_longitude = _safe_float_at(
                columns, _RUNWAY_END_ONE_LONGITUDE_COLUMN
            )
            end_two_latitude = _safe_float_at(
                columns, _RUNWAY_END_TWO_LATITUDE_COLUMN
            )
            end_two_longitude = _safe_float_at(
                columns, _RUNWAY_END_TWO_LONGITUDE_COLUMN
            )
            if (
                end_one_latitude is not None
                and end_one_longitude is not None
                and end_two_latitude is not None
                and end_two_longitude is not None
            ):
                self._runway_latitude = (end_one_latitude + end_two_latitude) / 2.0
                self._runway_longitude = (
                    end_one_longitude + end_two_longitude
                ) / 2.0
        elif row_type == "102" and self._helipad_latitude is None:
            helipad_latitude = _safe_float_at(columns, _HELIPAD_LATITUDE_COLUMN)
            helipad_longitude = _safe_float_at(columns, _HELIPAD_LONGITUDE_COLUMN)
            if helipad_latitude is not None and helipad_longitude is not None:
                self._helipad_latitude = helipad_latitude
                self._helipad_longitude = helipad_longitude

    def resolve_position(self) -> Optional[Tuple[float, float]]:
        """Return the best ``(latitude, longitude)`` for this airport."""
        if self._datum_latitude is not None and self._datum_longitude is not None:
            return (self._datum_latitude, self._datum_longitude)
        if self._runway_latitude is not None and self._runway_longitude is not None:
            return (self._runway_latitude, self._runway_longitude)
        if self._helipad_latitude is not None and self._helipad_longitude is not None:
            return (self._helipad_latitude, self._helipad_longitude)
        return None


def _safe_float(text: str) -> Optional[float]:
    """Parse a float, returning ``None`` on failure."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _safe_float_at(columns: List[str], index: int) -> Optional[float]:
    """Parse the float at ``columns[index]``, or ``None`` if absent/bad."""
    if index >= len(columns):
        return None
    return _safe_float(columns[index])


def find_airport_near(
    global_apt_dat_path: Path,
    latitude: float,
    longitude: float,
    max_kilometers: float = 5.0,
) -> Optional[str]:
    """Return the identifier of the nearest airport within range.

    Streams the Global Airports ``apt.dat`` (do not load it fully) and
    returns the identifier of the airport whose position is closest to
    ``(latitude, longitude)`` and within ``max_kilometers``. Returns
    ``None`` when no airport falls within range or the file is missing.
    """
    global_apt_dat_path = Path(global_apt_dat_path)
    if not global_apt_dat_path.is_file():
        return None

    nearest_identifier: Optional[str] = None
    nearest_distance_kilometres = max_kilometers
    current_airport: Optional[_AirportPositionAccumulator] = None

    def finalize(airport: Optional[_AirportPositionAccumulator]) -> None:
        nonlocal nearest_identifier, nearest_distance_kilometres
        if airport is None:
            return
        position = airport.resolve_position()
        if position is None:
            return
        airport_latitude, airport_longitude = position
        distance_kilometres = _haversine_kilometres(
            latitude, longitude, airport_latitude, airport_longitude
        )
        if distance_kilometres <= nearest_distance_kilometres:
            nearest_distance_kilometres = distance_kilometres
            nearest_identifier = airport.identifier

    with global_apt_dat_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            stripped_line = raw_line.strip()
            if not stripped_line:
                continue
            header_identifier = _parse_header_identifier(stripped_line)
            if header_identifier is not None:
                finalize(current_airport)
                current_airport = _AirportPositionAccumulator(header_identifier)
                continue
            if stripped_line == "99":
                finalize(current_airport)
                current_airport = None
                break
            if current_airport is not None:
                current_airport.consider_row(stripped_line.split())
        finalize(current_airport)

    return nearest_identifier


def extract_airport_from_global_apt_dat(
    global_apt_dat_path: Path,
    airport_icao: str,
) -> str:
    """Return the verbatim ``apt.dat`` block for ``airport_icao``.

    The block runs from the matching header row (row type 1/16/17 whose
    identifier column equals ``airport_icao``) up to, but not including,
    the next header row or the trailing ``99``. Returns an empty string
    when the airport is not present.
    """
    global_apt_dat_path = Path(global_apt_dat_path)
    if not global_apt_dat_path.is_file():
        return ""

    captured_lines: List[str] = []
    capturing = False
    with global_apt_dat_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line_without_newline = raw_line.rstrip("\n").rstrip("\r")
            stripped_line = line_without_newline.strip()
            header_identifier = _parse_header_identifier(stripped_line)
            if capturing:
                if header_identifier is not None or stripped_line == "99":
                    break
                captured_lines.append(line_without_newline)
            elif header_identifier == airport_icao:
                capturing = True
                captured_lines.append(line_without_newline)

    return "\n".join(captured_lines)


def write_pack_apt_dat(pack_directory: Path, airport_block: str) -> None:
    """Write ``<pack>/Earth nav data/apt.dat`` around ``airport_block``.

    The file gets the two-line header ("I", then a generation banner),
    the verbatim airport block, and the trailing ``99``. The file is
    newline-terminated.
    """
    pack_directory = Path(pack_directory)
    earth_nav_data_directory = pack_directory / "Earth nav data"
    earth_nav_data_directory.mkdir(parents=True, exist_ok=True)
    apt_dat_path = earth_nav_data_directory / "apt.dat"

    block_body = airport_block.strip("\n")
    lines = [
        "I",
        "1100 Generated by Ortho4XP MSFS airport converter",
    ]
    if block_body:
        lines.append(block_body)
    lines.append("99")
    apt_dat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _degree_padding_for_latitude(
    padding_meters: float, latitude: float
) -> Tuple[float, float]:
    """Return ``(latitude_padding, longitude_padding)`` in degrees.

    Longitude degrees shrink with latitude, so the east-west padding is
    scaled by ``cos(latitude)``.
    """
    latitude_padding_degrees = padding_meters / _METRES_PER_DEGREE_LATITUDE
    cosine_latitude = math.cos(math.radians(latitude))
    if abs(cosine_latitude) < 1e-9:
        # Near the poles longitude degrees collapse; clamp to avoid a
        # divide-by-zero and produce a very wide (but finite) box.
        cosine_latitude = 1e-9
    longitude_padding_degrees = padding_meters / (
        _METRES_PER_DEGREE_LATITUDE * cosine_latitude
    )
    return latitude_padding_degrees, longitude_padding_degrees


def _rectangles_overlap(
    first: Tuple[float, float, float, float],
    second: Tuple[float, float, float, float],
) -> bool:
    """Return ``True`` when two ``(west, south, east, north)`` boxes touch."""
    first_west, first_south, first_east, first_north = first
    second_west, second_south, second_east, second_north = second
    if first_west > second_east or second_west > first_east:
        return False
    if first_south > second_north or second_south > first_north:
        return False
    return True


def _union_rectangle(
    first: Tuple[float, float, float, float],
    second: Tuple[float, float, float, float],
) -> Tuple[float, float, float, float]:
    """Return the bounding ``(west, south, east, north)`` of two boxes."""
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def _placement_metre_extents(
    placement: PlacedObject,
) -> Tuple[float, float, float, float]:
    """Return ``(east_min, north_min, east_max, north_max)`` in metres.

    The placement's model footprint (``bounds_xz``, OBJ8 metres: +X east,
    +Z south at heading 0) is rotated by the true heading (clockwise from
    north) around the placement point; a placement without a footprint is
    a point (all zeros).
    """
    if placement.bounds_xz is None:
        return (0.0, 0.0, 0.0, 0.0)
    min_x, min_z, max_x, max_z = placement.bounds_xz
    heading_radians = math.radians(placement.heading_degrees_true)
    cosine = math.cos(heading_radians)
    sine = math.sin(heading_radians)
    easts: List[float] = []
    norths: List[float] = []
    for corner_x, corner_z in (
        (min_x, min_z), (min_x, max_z), (max_x, min_z), (max_x, max_z)
    ):
        east_local = corner_x
        north_local = -corner_z
        easts.append(east_local * cosine + north_local * sine)
        norths.append(north_local * cosine - east_local * sine)
    return (min(easts), min(norths), max(easts), max(norths))


def _placement_exclusion_box(
    placement: PlacedObject, padding_meters: float
) -> Tuple[float, float, float, float]:
    """Return one placement's padded ``(west, south, east, north)`` box.

    Covers the heading-rotated model footprint (when known) plus
    ``padding_meters`` on every side, converted to degrees at the
    placement latitude.
    """
    east_min, north_min, east_max, north_max = _placement_metre_extents(
        placement
    )
    # Per-metre degree factors at this latitude.
    latitude_factor, longitude_factor = _degree_padding_for_latitude(
        1.0, placement.latitude
    )
    return (
        placement.longitude + (east_min - padding_meters) * longitude_factor,
        placement.latitude + (north_min - padding_meters) * latitude_factor,
        placement.longitude + (east_max + padding_meters) * longitude_factor,
        placement.latitude + (north_max + padding_meters) * latitude_factor,
    )


def compute_exclusion_rectangles(
    placed_objects: List[PlacedObject],
    padding_meters: float = 20.0,
) -> List[Tuple[float, float, float, float]]:
    """Return degree rectangles suppressing default 3D under placements.

    Each placement is expanded into a padded box covering its model
    footprint rotated by the placement heading (falling back to the bare
    placement point when the footprint is unknown; the longitude padding
    is scaled by ``cos(latitude)``); overlapping boxes are merged. When
    all placements span less than two kilometres they collapse to a
    single bounding rectangle. Each rectangle is ``(west, south, east,
    north)`` in degrees.
    """
    if not placed_objects:
        return []

    padded_boxes = [
        _placement_exclusion_box(placement, padding_meters)
        for placement in placed_objects
    ]

    minimum_latitude = min(placement.latitude for placement in placed_objects)
    maximum_latitude = max(placement.latitude for placement in placed_objects)
    minimum_longitude = min(placement.longitude for placement in placed_objects)
    maximum_longitude = max(placement.longitude for placement in placed_objects)

    span_kilometres = _haversine_kilometres(
        minimum_latitude,
        minimum_longitude,
        maximum_latitude,
        maximum_longitude,
    )
    if span_kilometres < _SINGLE_RECTANGLE_SPAN_KILOMETRES:
        bounding = padded_boxes[0]
        for box in padded_boxes[1:]:
            bounding = _union_rectangle(bounding, box)
        return [bounding]

    merged_rectangles: List[Tuple[float, float, float, float]] = []
    for box in padded_boxes:
        current = box
        changed = True
        while changed:
            changed = False
            remaining: List[Tuple[float, float, float, float]] = []
            for existing in merged_rectangles:
                if _rectangles_overlap(current, existing):
                    current = _union_rectangle(current, existing)
                    changed = True
                else:
                    remaining.append(existing)
            merged_rectangles = remaining
        merged_rectangles.append(current)

    return merged_rectangles


def _tile_indices_for(longitude: float, latitude: float) -> Tuple[int, int]:
    """Return the ``(longitude_floor, latitude_floor)`` 1x1 tile indices."""
    return (math.floor(longitude), math.floor(latitude))


def _tile_stem(longitude_floor: int, latitude_floor: int) -> str:
    """Return the DSF file stem, for example ``"+44-122"``."""
    return "{:+03d}{:+04d}".format(latitude_floor, longitude_floor)


def _ten_degree_folder_name(longitude_floor: int, latitude_floor: int) -> str:
    """Return the 10-degree grid folder, for example ``"+40-130"``."""
    latitude_bucket = math.floor(latitude_floor / 10.0) * 10
    longitude_bucket = math.floor(longitude_floor / 10.0) * 10
    return "{:+03d}{:+04d}".format(latitude_bucket, longitude_bucket)


def _rectangle_intersects_tile(
    rectangle: Tuple[float, float, float, float],
    west: int,
    south: int,
    east: int,
    north: int,
) -> bool:
    """Return ``True`` when the rectangle overlaps the integer tile box."""
    rectangle_west, rectangle_south, rectangle_east, rectangle_north = rectangle
    if rectangle_west > east or west > rectangle_east:
        return False
    if rectangle_south > north or south > rectangle_north:
        return False
    return True


def _format_exclusion_value(rectangle: Tuple[float, float, float, float]) -> str:
    """Return the ``west/south/east/north`` slash-separated exclusion value.

    This slash form was verified by round-tripping through DSFTool
    (``--text2dsf`` followed by ``--dsf2text``): the exclude properties
    survive intact.
    """
    west, south, east, north = rectangle
    return "{:.9f}/{:.9f}/{:.9f}/{:.9f}".format(west, south, east, north)


def _build_tile_dsf_text(
    longitude_floor: int,
    latitude_floor: int,
    tile_placements: List[PlacedObject],
    tile_exclusion_rectangles: List[Tuple[float, float, float, float]],
) -> str:
    """Return the DSF2TEXT source for one 1x1 tile."""
    west = longitude_floor
    east = longitude_floor + 1
    south = latitude_floor
    north = latitude_floor + 1

    lines: List[str] = ["A", "800", "DSF2TEXT", ""]
    lines.append("PROPERTY sim/planet earth")
    lines.append("PROPERTY sim/overlay 1")
    lines.append("PROPERTY sim/require_object 1/0")
    for rectangle in tile_exclusion_rectangles:
        value = _format_exclusion_value(rectangle)
        lines.append("PROPERTY sim/exclude_obj " + value)
        lines.append("PROPERTY sim/exclude_fac " + value)
        lines.append("PROPERTY sim/exclude_agp " + value)
    lines.append("PROPERTY sim/west {:d}".format(west))
    lines.append("PROPERTY sim/east {:d}".format(east))
    lines.append("PROPERTY sim/north {:d}".format(north))
    lines.append("PROPERTY sim/south {:d}".format(south))

    unique_object_paths = sorted(
        {placement.object_relative_path for placement in tile_placements}
    )
    definition_index_by_path: Dict[str, int] = {}
    for definition_index, object_path in enumerate(unique_object_paths):
        definition_index_by_path[object_path] = definition_index
        lines.append("OBJECT_DEF " + object_path)

    for placement in tile_placements:
        definition_index = definition_index_by_path[placement.object_relative_path]
        if placement.altitude_meters == 0.0:
            # Ground-draped: the default overlay placement row.
            lines.append(
                "OBJECT {:d} {:.9f} {:.9f} {:.6f}".format(
                    definition_index,
                    placement.longitude,
                    placement.latitude,
                    placement.heading_degrees_true,
                )
            )
        else:
            row_type = "OBJECT_AGL" if placement.is_above_ground else "OBJECT_MSL"
            lines.append(
                "{} {:d} {:.9f} {:.9f} {:.6f} {:.3f}".format(
                    row_type,
                    definition_index,
                    placement.longitude,
                    placement.latitude,
                    placement.heading_degrees_true,
                    placement.altitude_meters,
                )
            )

    return "\n".join(lines) + "\n"


def write_overlay_dsf(
    pack_directory: Path,
    placed_objects: List[PlacedObject],
    exclusion_rectangles: List[Tuple[float, float, float, float]],
    dsftool_path: Path,
) -> Path:
    """Write the overlay DSF(s) for a pack and return the nav-data dir.

    Placements are grouped by 1x1-degree tile; each tile is written to
    ``<pack>/Earth nav data/<10deg folder>/<tile>.dsf`` via
    ``DSFTool --text2dsf``. Every tile receives the OBJECT_DEFs it needs
    and any exclusion rectangle overlapping its bounds. Returns the pack
    ``Earth nav data`` directory.

    Raises ``RuntimeError`` when DSFTool fails for a tile.
    """
    pack_directory = Path(pack_directory)
    dsftool_path = Path(dsftool_path)
    earth_nav_data_directory = pack_directory / "Earth nav data"
    earth_nav_data_directory.mkdir(parents=True, exist_ok=True)

    if not placed_objects:
        return earth_nav_data_directory

    placements_by_tile: Dict[Tuple[int, int], List[PlacedObject]] = {}
    for placement in placed_objects:
        tile_indices = _tile_indices_for(placement.longitude, placement.latitude)
        placements_by_tile.setdefault(tile_indices, []).append(placement)

    for (longitude_floor, latitude_floor), tile_placements in sorted(
        placements_by_tile.items()
    ):
        west = longitude_floor
        east = longitude_floor + 1
        south = latitude_floor
        north = latitude_floor + 1
        tile_exclusion_rectangles = [
            rectangle
            for rectangle in exclusion_rectangles
            if _rectangle_intersects_tile(rectangle, west, south, east, north)
        ]
        dsf_text = _build_tile_dsf_text(
            longitude_floor,
            latitude_floor,
            tile_placements,
            tile_exclusion_rectangles,
        )

        folder_name = _ten_degree_folder_name(longitude_floor, latitude_floor)
        tile_stem = _tile_stem(longitude_floor, latitude_floor)
        tile_folder = earth_nav_data_directory / folder_name
        tile_folder.mkdir(parents=True, exist_ok=True)
        dsf_path = tile_folder / (tile_stem + ".dsf")

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            dir=str(tile_folder),
            delete=False,
            encoding="utf-8",
        ) as text_handle:
            text_handle.write(dsf_text)
            text_path = Path(text_handle.name)

        try:
            try:
                completed = subprocess.run(
                    [
                        str(dsftool_path),
                        "--text2dsf",
                        str(text_path),
                        str(dsf_path),
                    ],
                    capture_output=True,
                    text=True,
                )
            except OSError as error:
                raise RuntimeError(
                    "could not run DSFTool at {} for tile {}: {}".format(
                        dsftool_path, tile_stem, error
                    )
                ) from error
            if completed.returncode != 0:
                raise RuntimeError(
                    "DSFTool --text2dsf failed for tile {} (exit {}):\n{}\n{}".format(
                        tile_stem,
                        completed.returncode,
                        completed.stdout,
                        completed.stderr,
                    )
                )
        finally:
            try:
                text_path.unlink()
            except OSError:
                pass

    return earth_nav_data_directory
