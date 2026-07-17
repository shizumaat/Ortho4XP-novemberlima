"""Offline airport search index built from X-Plane's Global Airports.

This module builds a small, fast, offline search index of airports from
X-Plane's ``apt.dat`` (the Global Airports gateway data set).  The real
``apt.dat`` is hundreds of megabytes, so parsing is done strictly
line-by-line (streaming) and never loads the whole file into memory.

The public surface is intentionally tiny and stdlib-only (no tkinter/Qt,
no network access, no printing):

* :func:`find_apt_dats` -- locate the Global Airports ``apt.dat`` file(s).
* :func:`build_index`   -- stream-parse ``apt.dat`` into a compact cache.
* :func:`load_index`    -- fast reload from that cache.
* :func:`index_is_stale` -- decide whether the cache needs rebuilding.
* :func:`search`        -- rank airports for a free-text query.
* :func:`parse_coordinate_query` -- interpret a query as tile coordinates.

The cache is a compact TSV file (see :func:`build_index`) so reloads are
cheap and the on-disk format is easy to inspect.

Cache format v2 and the freshness contract
-------------------------------------------
The cache header is ``O4AIRPORTIDX 2 <count>``.  Immediately after the
header, :func:`build_index` writes one ``#SRC <mtime_ns> <size_bytes>
<path>`` line for every source ``apt.dat`` it actually read (the path is
written last because it may contain spaces; ``mtime_ns`` comes from
:func:`os.stat`'s ``st_mtime_ns`` for full precision, and the byte size is
recorded as a second freshness signal).  The tab-separated data rows are
unchanged from v1.  :func:`load_index` transparently loads both v1 (no
``#SRC`` lines) and v2 caches, skipping any ``#SRC`` lines.
:func:`index_is_stale` reads only the header and ``#SRC`` lines to report
whether the recorded sources still match the requested ones (same set of
paths, and identical ``st_mtime_ns`` and size for each), so callers can
rebuild only when something has actually changed on disk.
"""

import os
import sys
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional, Tuple

__all__ = [
    "AirportEntry",
    "find_apt_dats",
    "build_index",
    "load_index",
    "index_is_stale",
    "search",
    "parse_coordinate_query",
]

# Magic header written as the first line of the cache file.  The integer
# is a format version so a future change can invalidate old caches.
#
# Version 2 adds ``#SRC <mtime_ns> <size_bytes> <path>`` lines right after
# the header, recording every source file used to build the index so
# :func:`index_is_stale` can tell when a rebuild is needed.
_CACHE_MAGIC = "O4AIRPORTIDX"
_CACHE_VERSION = 3

# Prefix marking a source-file provenance line in a v2+ cache.
_SRC_PREFIX = "#SRC"

# The v3 per-airport category values (the bathymetry gate's anchor
# checkboxes select among these):
#   icao_airport  -- apt.dat type 1 with an ``icao_code`` metadata row
#   airport       -- apt.dat type 1 without one (small strips, local IDs)
#   seaplane_base -- apt.dat type 16
#   heliport      -- apt.dat type 17
AIRPORT_CATEGORIES = (
    "icao_airport", "airport", "seaplane_base", "heliport",
)


@dataclass
class AirportEntry:
    """A single indexed airport.

    Attributes:
        code: ICAO code (or the ``apt.dat`` header airport ID when no
            ``icao_code`` metadata row is present).
        name: Human-readable airport name.
        city: City the airport serves, or ``""`` if unknown.
        country: Country the airport is in, or ``""`` if unknown.
        lat: Reference latitude in decimal degrees.
        lon: Reference longitude in decimal degrees.
        category: One of :data:`AIRPORT_CATEGORIES`.  Rows loaded from a
            pre-v3 cache carry the default (they predate the category
            column; ``index_is_stale`` rebuilds such caches anyway).
    """

    code: str
    name: str
    city: str
    country: str
    lat: float
    lon: float
    category: str = "icao_airport"


# ---------------------------------------------------------------------------
# Locating apt.dat
# ---------------------------------------------------------------------------
def find_apt_dats(xplane_dir: str) -> List[str]:
    """Return existing Global Airports ``apt.dat`` paths under ``xplane_dir``.

    Two well-known locations are checked, in priority order:

    1. ``<xp>/Global Scenery/Global Airports/Earth nav data/apt.dat`` (XP12)
    2. ``<xp>/Custom Scenery/Global Airports/Earth nav data/apt.dat`` (XP11)

    Args:
        xplane_dir: Path to the X-Plane installation root.

    Returns:
        A list of the ``apt.dat`` paths that actually exist, in the order
        above.  Returns ``[]`` when none are found.
    """
    candidates = [
        os.path.join(
            xplane_dir, "Global Scenery", "Global Airports",
            "Earth nav data", "apt.dat"),
        os.path.join(
            xplane_dir, "Custom Scenery", "Global Airports",
            "Earth nav data", "apt.dat"),
    ]
    return [p for p in candidates if os.path.isfile(p)]


# ---------------------------------------------------------------------------
# apt.dat parsing (streaming)
# ---------------------------------------------------------------------------
# Airport header row codes.
_HEADER_CODES = frozenset(("1", "16", "17"))


def _airport_category(header_code: str, saw_icao_code: bool) -> str:
    """Map an apt.dat header row code (+ icao_code presence) to a
    :data:`AIRPORT_CATEGORIES` value."""
    if header_code == "16":
        return "seaplane_base"
    if header_code == "17":
        return "heliport"
    return "icao_airport" if saw_icao_code else "airport"


def _flush_airport(
    code: Optional[str],
    name: str,
    city: str,
    country: str,
    meta_lat: Optional[float],
    meta_lon: Optional[float],
    rwy_lat: Optional[float],
    rwy_lon: Optional[float],
    category: str = "icao_airport",
) -> Optional[AirportEntry]:
    """Assemble an :class:`AirportEntry` from an airport's accumulated rows.

    Metadata datum coordinates win over the runway/helipad fallback.
    Returns ``None`` when no code or no usable coordinate is available, so
    the caller can skip the airport.
    """
    if not code:
        return None
    lat = meta_lat if meta_lat is not None else rwy_lat
    lon = meta_lon if meta_lon is not None else rwy_lon
    if lat is None or lon is None:
        return None
    return AirportEntry(code=code, name=name, city=city, country=country,
                        lat=lat, lon=lon, category=category)


def _parse_float(value: str) -> Optional[float]:
    """Return ``value`` parsed as ``float`` or ``None`` if malformed."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iter_airports(path: str) -> Iterator[AirportEntry]:
    """Yield :class:`AirportEntry` objects streamed from one ``apt.dat``.

    The file is read one line at a time (never fully into memory).  Rows
    are interpreted as follows:

    * ``1``/``16``/``17`` -- airport header, ``<code> <elev> ... <ID> <Name>``.
    * ``1302 <key> <value>`` -- metadata; keys ``icao_code`` (overrides the
      header ID), ``city``, ``country``, ``datum_lat``, ``datum_lon``.
    * ``100`` -- land runway; end-1 lat/lon are fields 9 and 10 (0-based).
    * ``102`` -- helipad; lat/lon are fields 2 and 3 (0-based).

    An airport with no metadata datum and no runway/helipad coordinate is
    skipped (not yielded).
    """
    # Accumulator state for the airport currently being parsed.
    have_airport = False
    code: Optional[str] = None
    name = ""
    city = ""
    country = ""
    header_code = "1"
    saw_icao_code = False
    meta_lat: Optional[float] = None
    meta_lon: Optional[float] = None
    rwy_lat: Optional[float] = None
    rwy_lon: Optional[float] = None

    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            row = line.split()
            row_code = row[0]

            if row_code in _HEADER_CODES:
                # New airport begins: flush the previous one first.
                if have_airport:
                    entry = _flush_airport(
                        code, name, city, country,
                        meta_lat, meta_lon, rwy_lat, rwy_lon,
                        _airport_category(header_code, saw_icao_code))
                    if entry is not None:
                        yield entry
                # Reset accumulators for the new header.
                have_airport = True
                city = ""
                country = ""
                header_code = row_code
                saw_icao_code = False
                meta_lat = None
                meta_lon = None
                rwy_lat = None
                rwy_lon = None
                # Header layout: 1 <elev> <dep> <dep> <ID> <Name...>
                code = row[4] if len(row) >= 5 else None
                name = " ".join(row[5:]) if len(row) >= 6 else ""
                continue

            if not have_airport:
                # Rows before the first header (e.g. file preamble) are
                # not part of any airport; ignore them.
                continue

            if row_code == "1302" and len(row) >= 3:
                key = row[1]
                value = " ".join(row[2:])
                if key == "icao_code":
                    if value:
                        code = value
                        saw_icao_code = True
                elif key == "city":
                    city = value
                elif key == "country":
                    country = value
                elif key == "datum_lat":
                    parsed = _parse_float(value)
                    if parsed is not None:
                        meta_lat = parsed
                elif key == "datum_lon":
                    parsed = _parse_float(value)
                    if parsed is not None:
                        meta_lon = parsed
                continue

            # Runway / helipad coordinate fallbacks -- only the FIRST such
            # row is used (datum metadata still overrides these anyway).
            if row_code == "100" and rwy_lat is None and len(row) >= 11:
                lat = _parse_float(row[9])
                lon = _parse_float(row[10])
                if lat is not None and lon is not None:
                    rwy_lat = lat
                    rwy_lon = lon
                continue

            if row_code == "102" and rwy_lat is None and len(row) >= 4:
                lat = _parse_float(row[2])
                lon = _parse_float(row[3])
                if lat is not None and lon is not None:
                    rwy_lat = lat
                    rwy_lon = lon
                continue

    # Flush the trailing airport at end-of-file.
    if have_airport:
        entry = _flush_airport(
            code, name, city, country,
            meta_lat, meta_lon, rwy_lat, rwy_lon,
            _airport_category(header_code, saw_icao_code))
        if entry is not None:
            yield entry


# ---------------------------------------------------------------------------
# Cache (build / load)
# ---------------------------------------------------------------------------
def _sanitize(value: str) -> str:
    """Strip TSV-hostile characters (tabs/newlines) from a cache field."""
    return value.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def build_index(apt_dat_paths: Iterable[str], cache_file: str) -> int:
    """Stream-parse ``apt.dat`` file(s) into a compact TSV cache.

    Each airport is parsed once; when the same airport code appears in more
    than one file the FIRST occurrence wins (so callers should list the
    highest-priority file -- typically the XP12 path -- first).

    The cache is a UTF-8 TSV file whose first line is::

        O4AIRPORTIDX 2 <count>

    followed by one ``#SRC <mtime_ns> <size_bytes> <path>`` line per source
    file actually read, then one tab-separated ``code<TAB>name<TAB>city<TAB>
    country<TAB>lat<TAB>lon`` row per airport.  The file is written
    atomically (to a temporary file then :func:`os.replace`).

    Args:
        apt_dat_paths: Ordered iterable of ``apt.dat`` paths to index.
        cache_file: Destination cache path.

    Returns:
        The number of airports written to the cache.
    """
    seen: set = set()
    rows: List[AirportEntry] = []
    # (path, mtime_ns, size_bytes) for every source we actually read, in the
    # order read, so v2 caches can record their freshness signals.
    sources: List[Tuple[str, int, int]] = []
    for path in apt_dat_paths:
        if not path or not os.path.isfile(path):
            continue
        stat = os.stat(path)
        sources.append((path, stat.st_mtime_ns, stat.st_size))
        for entry in _iter_airports(path):
            if entry.code in seen:
                continue
            seen.add(entry.code)
            rows.append(entry)

    tmp_file = cache_file + ".tmp"
    directory = os.path.dirname(cache_file)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)

    with open(tmp_file, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("{} {} {}\n".format(
            _CACHE_MAGIC, _CACHE_VERSION, len(rows)))
        for path, mtime_ns, size in sources:
            # Path is written last (and verbatim) because it may contain
            # spaces; tabs/newlines are stripped so the line stays parseable.
            handle.write("{} {} {} {}\n".format(
                _SRC_PREFIX, mtime_ns, size, _sanitize(path)))
        for e in rows:
            handle.write("\t".join((
                _sanitize(e.code),
                _sanitize(e.name),
                _sanitize(e.city),
                _sanitize(e.country),
                repr(e.lat),
                repr(e.lon),
                _sanitize(e.category),
            )) + "\n")
    os.replace(tmp_file, cache_file)
    return len(rows)


def load_index(cache_file: str) -> List[AirportEntry]:
    """Load airports from a cache written by :func:`build_index`.

    The cache is streamed line-by-line.  A missing file or a file without
    the expected magic header yields an empty list.  v1 (no ``#SRC``
    lines), v2 and v3 caches are all accepted; ``#SRC`` provenance lines
    are skipped, and pre-v3 rows (no category column) load with the
    :class:`AirportEntry` category default.  Malformed data rows are
    skipped rather than raising.

    Args:
        cache_file: Path to a cache produced by :func:`build_index`.

    Returns:
        The list of :class:`AirportEntry` objects in file order.
    """
    entries: List[AirportEntry] = []
    if not os.path.isfile(cache_file):
        return entries
    with open(cache_file, "r", encoding="utf-8", errors="replace") as handle:
        first = handle.readline()
        if not first.startswith(_CACHE_MAGIC):
            return entries
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            if line.startswith(_SRC_PREFIX):
                # v2 source-provenance line -- not an airport entry.
                continue
            parts = line.split("\t")
            if len(parts) not in (6, 7):
                continue
            lat = _parse_float(parts[4])
            lon = _parse_float(parts[5])
            if lat is None or lon is None:
                continue
            entry = AirportEntry(
                code=parts[0], name=parts[1], city=parts[2],
                country=parts[3], lat=lat, lon=lon)
            if len(parts) == 7 and parts[6] in AIRPORT_CATEGORIES:
                entry.category = parts[6]
            entries.append(entry)
    return entries


def index_is_stale(apt_dat_paths: List[str], cache_file: str) -> bool:
    """Return ``True`` when ``cache_file`` needs rebuilding from the sources.

    Only the header and the ``#SRC`` provenance lines of the cache are read
    (never the airport rows), so this is cheap to call on every run.  The
    cache is considered stale -- i.e. this returns ``True`` -- when ANY of
    the following hold:

    * the cache file is missing or unreadable;
    * the header is malformed (missing/incorrect magic or version);
    * the header version is below the current one (v1 caches carry no
      source info; pre-v3 caches carry no airport categories);
    * the set of recorded source paths differs from ``apt_dat_paths``
      (compared order-insensitively after :func:`os.path.abspath`);
    * a recorded source no longer exists on disk;
    * a source's current ``st_mtime_ns`` or byte size differs from the
      recorded value (compared with ``!=`` so a restored/downgraded file
      also triggers a rebuild).

    It returns ``False`` only when every recorded source still matches.  An
    empty ``apt_dat_paths`` against a v2 cache that itself recorded zero
    sources is *fresh* (there is nothing to compare); empty paths with no
    cache is stale.

    Args:
        apt_dat_paths: The source ``apt.dat`` paths the cache should cover.
        cache_file: Path to a cache produced by :func:`build_index`.

    Returns:
        ``True`` if a rebuild is warranted, ``False`` otherwise.
    """
    desired = {os.path.abspath(p) for p in apt_dat_paths if p}

    if not os.path.isfile(cache_file):
        return True

    recorded: dict = {}
    try:
        with open(cache_file, "r", encoding="utf-8",
                  errors="replace") as handle:
            first = handle.readline()
            tokens = first.split()
            if len(tokens) < 2 or tokens[0] != _CACHE_MAGIC:
                return True
            try:
                version = int(tokens[1])
            except ValueError:
                return True
            if version < _CACHE_VERSION:
                # Pre-v3 caches carry no airport categories (v1 also no
                # source info): rebuild once to gain the new column.
                return True
            # Read only the leading #SRC provenance lines.
            for line in handle:
                line = line.rstrip("\n")
                if not line.startswith(_SRC_PREFIX):
                    break
                parts = line.split(None, 3)
                if len(parts) != 4:
                    return True
                try:
                    mtime_ns = int(parts[1])
                    size = int(parts[2])
                except ValueError:
                    return True
                recorded[os.path.abspath(parts[3])] = (mtime_ns, size)
    except OSError:
        return True

    if set(recorded) != desired:
        return True

    for abs_path, (mtime_ns, size) in recorded.items():
        if not os.path.exists(abs_path):
            return True
        try:
            stat = os.stat(abs_path)
        except OSError:
            return True
        if stat.st_mtime_ns != mtime_ns or stat.st_size != size:
            return True

    return False


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
# Rank buckets (lower = better match).
_RANK_EXACT_CODE = 0
_RANK_CODE_PREFIX = 1
_RANK_NAME_PREFIX = 2
_RANK_NAME_SUBSTR = 3
_RANK_CITY_SUBSTR = 4
_RANK_COUNTRY_SUBSTR = 5


def _match_rank(entry: AirportEntry, query: str) -> Optional[int]:
    """Return the best (lowest) rank bucket for ``entry`` vs ``query``.

    ``query`` must already be lower-cased.  Returns ``None`` when the entry
    does not match at all.
    """
    code = entry.code.lower()
    name = entry.name.lower()
    city = entry.city.lower()
    country = entry.country.lower()

    if code == query:
        return _RANK_EXACT_CODE
    if code.startswith(query):
        return _RANK_CODE_PREFIX
    if name.startswith(query):
        return _RANK_NAME_PREFIX
    if query in name:
        return _RANK_NAME_SUBSTR
    if query in city:
        return _RANK_CITY_SUBSTR
    if query in country:
        return _RANK_COUNTRY_SUBSTR
    return None


def search(entries: List[AirportEntry], query: str,
           limit: int = 10) -> List[AirportEntry]:
    """Return up to ``limit`` airports matching ``query``, best first.

    Matching is case-insensitive and ranked in this order: exact code
    match, code prefix, name prefix, substring within name, substring
    within city, then substring within country.  Ties within a rank keep
    the input order (stable sort).

    Queries shorter than two characters return ``[]``.

    Args:
        entries: Airports to search (typically from :func:`load_index`).
        query: Free-text query.
        limit: Maximum number of results to return.

    Returns:
        The matching airports, ranked and truncated to ``limit``.
    """
    q = query.strip().lower()
    if len(q) < 2:
        return []
    scored: List[Tuple[int, int, AirportEntry]] = []
    for idx, entry in enumerate(entries):
        rank = _match_rank(entry, q)
        if rank is not None:
            scored.append((rank, idx, entry))
    # (rank, original index) is a total order that is stable within a rank.
    scored.sort(key=lambda t: (t[0], t[1]))
    return [entry for _, _, entry in scored[:max(0, limit)]]


# ---------------------------------------------------------------------------
# Coordinate query
# ---------------------------------------------------------------------------
import math  # noqa: E402  (kept local to the coordinate helper's concerns)

# Valid X-Plane tile ranges (integer south-west corner of a 1x1 tile).
_LAT_MIN, _LAT_MAX = -85, 84
_LON_MIN, _LON_MAX = -180, 179


def _validate_tile(lat: int, lon: int) -> Optional[Tuple[int, int]]:
    """Return ``(lat, lon)`` if within valid tile ranges, else ``None``."""
    if _LAT_MIN <= lat <= _LAT_MAX and _LON_MIN <= lon <= _LON_MAX:
        return (lat, lon)
    return None


def parse_coordinate_query(query: str) -> Optional[Tuple[int, int]]:
    """Interpret ``query`` as an explicit tile coordinate, if possible.

    Accepted forms (floats are floored to the containing tile integer)::

        "48 -6"      "48,-6"      "48.7 -5.2"
        "+48-006"    "-34+151"

    Latitude must fall in ``[-85, 84]`` and longitude in ``[-180, 179]``
    (the valid X-Plane tile ranges).  Anything else returns ``None``.

    Args:
        query: The raw query string.

    Returns:
        ``(lat, lon)`` integer tile coordinates, or ``None`` when the query
        is not a valid coordinate pair.
    """
    if query is None:
        return None
    text = query.strip()
    if not text:
        return None

    # Form 1: two numbers separated by whitespace and/or a comma.
    tokens = [t for t in text.replace(",", " ").split() if t]
    if len(tokens) == 2:
        lat_f = _parse_float(tokens[0])
        lon_f = _parse_float(tokens[1])
        if lat_f is not None and lon_f is not None:
            return _validate_tile(
                int(math.floor(lat_f)), int(math.floor(lon_f)))
        return None

    # Form 2: signed concatenation like "+48-006" or "-34+151".  The
    # longitude sign is the first '+'/'-' after position 0.
    if len(tokens) == 1:
        token = tokens[0]
        if len(token) >= 2 and token[0] in "+-":
            split_at = -1
            for i in range(1, len(token)):
                if token[i] in "+-":
                    split_at = i
                    break
            if split_at > 0:
                lat_f = _parse_float(token[:split_at])
                lon_f = _parse_float(token[split_at:])
                if lat_f is not None and lon_f is not None:
                    return _validate_tile(
                        int(math.floor(lat_f)), int(math.floor(lon_f)))
    return None
