"""Unit tests for :mod:`O4_Airport_Index`.

All tests are self-contained: a synthetic ``apt.dat`` fixture string is
written into ``tmp_path`` so nothing depends on a real X-Plane install or
the network.  The module is imported directly -- ``tests/conftest.py``
already inserts the project ``src/`` directory onto ``sys.path``.
"""
import os
import sys

import pytest

# conftest.py adds ../src to sys.path; keep an explicit fallback so this
# module also imports when run in isolation.
_SRC = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import O4_Airport_Index as AI  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------
# AAAA -- full 1302 metadata incl. icao_code override, city, country, datum.
# BBBB -- no metadata; reference coords come from a land runway (100).
# HHHH -- heliport (row code 17) with a helipad (102) fallback.
# SSSS -- seaplane base (row code 16) with ONLY a water runway (101); this
#         module implements only 100/102 fallbacks, so it must be skipped.
# NNNN -- land airport with no coordinate row at all; must be skipped.
_APT_DAT_1 = """I
1000 Generter apt.dat

1    100 0 0 XXXX Header Should Be Overridden
1302 icao_code AAAA
1302 city Testville
1302 country Testland
1302 datum_lat 48.5
1302 datum_lon -6.25
100 45.0 1 0 0.25 1 3 0 01 48.4999 -6.2501 0 0 0 0 0 0 19 48.5100 -6.2400 0 0 0 0 0 0

1    50 0 0 BBBB Runway Fallback Field
100 30.0 1 0 0.25 1 3 0 09 12.5000 77.7000 0 0 0 0 0 0 27 12.5100 77.7100 0 0 0 0 0 0

17   20 0 0 HHHH Heliport Fallback
102 H1 51.5000 -0.1000 0 20 20 1 0 0 0.5

16   0 0 0 SSSS Seaplane No Land Runway
101 60 1 07 59.0000 10.0000 25 58.9000 10.1000

1    10 0 0 NNNN No Coordinates Here
1302 city Nowhere
"""

# Second file: AAAA duplicated (different name) to prove first-file-wins,
# plus a unique airport CCCC.
_APT_DAT_2 = """I
1000 Generter apt.dat

1    100 0 0 XXXX Duplicate Should Lose
1302 icao_code AAAA
1302 datum_lat 10.0
1302 datum_lon 10.0

1    5 0 0 CCCC Second File Only
1302 datum_lat -33.9000
1302 datum_lon 151.2000
"""


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return str(path)


@pytest.fixture()
def apt1(tmp_path):
    return _write(tmp_path / "apt1.dat", _APT_DAT_1)


@pytest.fixture()
def apt2(tmp_path):
    return _write(tmp_path / "apt2.dat", _APT_DAT_2)


# ---------------------------------------------------------------------------
# find_apt_dats
# ---------------------------------------------------------------------------
def test_find_apt_dats_empty(tmp_path):
    assert AI.find_apt_dats(str(tmp_path)) == []


def test_find_apt_dats_priority_order(tmp_path):
    xp12 = tmp_path / "Global Scenery" / "Global Airports" / "Earth nav data"
    xp11 = tmp_path / "Custom Scenery" / "Global Airports" / "Earth nav data"
    os.makedirs(xp12)
    os.makedirs(xp11)
    p12 = _write(xp12 / "apt.dat", "I\n")
    p11 = _write(xp11 / "apt.dat", "I\n")
    found = AI.find_apt_dats(str(tmp_path))
    assert found == [p12, p11]


def test_find_apt_dats_only_xp11(tmp_path):
    xp11 = tmp_path / "Custom Scenery" / "Global Airports" / "Earth nav data"
    os.makedirs(xp11)
    p11 = _write(xp11 / "apt.dat", "I\n")
    assert AI.find_apt_dats(str(tmp_path)) == [p11]


# ---------------------------------------------------------------------------
# build_index / load_index round-trip
# ---------------------------------------------------------------------------
def test_build_index_count_and_skips(apt1, tmp_path):
    cache = str(tmp_path / "index.tsv")
    count = AI.build_index([apt1], cache)
    # AAAA + BBBB + HHHH indexed; SSSS (water only) and NNNN (no coords)
    # are skipped -> 3.
    assert count == 3
    assert os.path.isfile(cache)
    codes = {e.code for e in AI.load_index(cache)}
    assert codes == {"AAAA", "BBBB", "HHHH"}
    assert "SSSS" not in codes  # seaplane / water runway not implemented
    assert "NNNN" not in codes  # no coordinate row


def test_cache_header_line(apt1, tmp_path):
    cache = str(tmp_path / "index.tsv")
    AI.build_index([apt1], cache)
    with open(cache, encoding="utf-8") as fh:
        header = fh.readline().strip()
    assert header == "O4AIRPORTIDX 1 3"


def test_field_integrity_roundtrip(apt1, tmp_path):
    cache = str(tmp_path / "index.tsv")
    AI.build_index([apt1], cache)
    by_code = {e.code: e for e in AI.load_index(cache)}

    a = by_code["AAAA"]
    # icao_code metadata overrides the header ID "XXXX".
    assert a.code == "AAAA"
    assert a.name == "Header Should Be Overridden"
    assert a.city == "Testville"
    assert a.country == "Testland"
    assert a.lat == pytest.approx(48.5)
    assert a.lon == pytest.approx(-6.25)

    b = by_code["BBBB"]
    assert b.city == ""
    assert b.country == ""
    # Falls back to runway-100 end-1 coords (fields 9,10).
    assert b.lat == pytest.approx(12.5000)
    assert b.lon == pytest.approx(77.7000)

    h = by_code["HHHH"]
    # Helipad-102 fallback (fields 2,3).
    assert h.lat == pytest.approx(51.5000)
    assert h.lon == pytest.approx(-0.1000)


def test_load_missing_cache(tmp_path):
    assert AI.load_index(str(tmp_path / "nope.tsv")) == []


def test_atomic_write_leaves_no_tmp(apt1, tmp_path):
    cache = str(tmp_path / "index.tsv")
    AI.build_index([apt1], cache)
    assert not os.path.isfile(cache + ".tmp")


# ---------------------------------------------------------------------------
# Duplicate handling across files (first file wins)
# ---------------------------------------------------------------------------
def test_duplicate_first_file_wins(apt1, apt2, tmp_path):
    cache = str(tmp_path / "index.tsv")
    count = AI.build_index([apt1, apt2], cache)
    by_code = {e.code: e for e in AI.load_index(cache)}
    # AAAA appears in both; the apt1 version (datum 48.5/-6.25) must win.
    assert by_code["AAAA"].lat == pytest.approx(48.5)
    assert by_code["AAAA"].name == "Header Should Be Overridden"
    # CCCC is unique to apt2.
    assert "CCCC" in by_code
    assert by_code["CCCC"].lat == pytest.approx(-33.9)
    # 3 from apt1 + 1 new from apt2 (AAAA duplicate not recounted).
    assert count == 4


# ---------------------------------------------------------------------------
# search ranking
# ---------------------------------------------------------------------------
def _entries():
    return [
        AI.AirportEntry("KJFK", "John F Kennedy Intl", "New York",
                        "United States", 40.6, -73.8),
        AI.AirportEntry("KJFA", "Jefferson Airfield", "Jeffrey",
                        "United States", 39.0, -95.0),
        AI.AirportEntry("EGLL", "London Heathrow", "London",
                        "United Kingdom", 51.5, -0.5),
        AI.AirportEntry("LFPG", "Charles de Gaulle Kennedy Annex", "Paris",
                        "France", 49.0, 2.5),
        AI.AirportEntry("ZZKJ", "Zeta Field", "Kjburg", "Kjland",
                        1.0, 1.0),
    ]


def test_search_exact_code_first():
    res = AI.search(_entries(), "KJFK")
    assert res[0].code == "KJFK"


def test_search_code_prefix_beats_name_substring():
    # "KJ" is a code prefix for KJFK/KJFA, and also a name/city/country
    # substring elsewhere; code-prefix matches must sort ahead.
    res = AI.search(_entries(), "KJ")
    top_two = {res[0].code, res[1].code}
    assert top_two == {"KJFK", "KJFA"}


def test_search_name_prefix_and_substring_order():
    res = AI.search(_entries(), "kennedy")
    codes = [e.code for e in res]
    # "John F Kennedy" contains kennedy as a substring; no name STARTS with
    # kennedy, so both KJFK and LFPG are name-substring rank, input order.
    assert codes.index("KJFK") < codes.index("LFPG")


def test_search_exact_beats_prefix():
    entries = [
        AI.AirportEntry("KJFKX", "Prefix Match", "", "", 0.0, 0.0),
        AI.AirportEntry("KJFK", "Exact Match", "", "", 0.0, 0.0),
    ]
    res = AI.search(entries, "kjfk")
    assert res[0].code == "KJFK"
    assert res[1].code == "KJFKX"


def test_search_limit():
    entries = [AI.AirportEntry("AA%02d" % i, "Alpha %d" % i, "", "",
                               0.0, 0.0) for i in range(20)]
    res = AI.search(entries, "aa", limit=5)
    assert len(res) == 5


def test_search_short_query_returns_empty():
    assert AI.search(_entries(), "k") == []
    assert AI.search(_entries(), "") == []


def test_search_stable_within_rank():
    entries = [
        AI.AirportEntry("ZZ01", "Foobar One", "", "", 0.0, 0.0),
        AI.AirportEntry("ZZ02", "Foobar Two", "", "", 0.0, 0.0),
        AI.AirportEntry("ZZ03", "Foobar Three", "", "", 0.0, 0.0),
    ]
    res = AI.search(entries, "foobar")
    assert [e.code for e in res] == ["ZZ01", "ZZ02", "ZZ03"]


# ---------------------------------------------------------------------------
# parse_coordinate_query
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("query,expected", [
    ("48 -6", (48, -6)),
    ("48,-6", (48, -6)),
    ("+48-006", (48, -6)),
    ("-34+151", (-34, 151)),
    ("48.7 -5.2", (48, -6)),        # floats floored to the containing tile
    ("0 0", (0, 0)),
    ("-1 -1", (-1, -1)),
    ("-85 -180", (-85, -180)),      # inclusive lower bounds
    ("84 179", (84, 179)),          # inclusive upper bounds
])
def test_parse_coordinate_query_valid(query, expected):
    assert AI.parse_coordinate_query(query) == expected


@pytest.mark.parametrize("query", [
    "abc",
    "99 200",       # both out of range
    "90 0",         # lat too high
    "48 999",       # lon too high
    "-86 0",        # lat too low
    "48",           # single number, no lon
    "",
    "   ",
    "48 -6 12",     # three tokens
])
def test_parse_coordinate_query_invalid(query):
    assert AI.parse_coordinate_query(query) is None


# ---------------------------------------------------------------------------
# Full pipeline: find -> build -> load -> search
# ---------------------------------------------------------------------------
def test_end_to_end(tmp_path):
    xp12 = tmp_path / "Global Scenery" / "Global Airports" / "Earth nav data"
    os.makedirs(xp12)
    _write(xp12 / "apt.dat", _APT_DAT_1)
    paths = AI.find_apt_dats(str(tmp_path))
    assert len(paths) == 1
    cache = str(tmp_path / "cache.tsv")
    n = AI.build_index(paths, cache)
    assert n == 3
    entries = AI.load_index(cache)
    hit = AI.search(entries, "AAAA")
    assert hit and hit[0].code == "AAAA"
