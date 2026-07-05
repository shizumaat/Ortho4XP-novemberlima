"""Unit tests for O4_Apt_Dat_Reader.

Uses ``tests/fixtures/synthetic_apt.dat`` which contains two
hand-crafted airport blocks:

* ``ZZZZ`` — Test Airport One.  One runway, three pavements (a
  plain square, a square with one Bezier corner, and a square with
  an interior hole) and a boundary polygon.
* ``YYYY`` — minimal stub used to verify the parser stops at the
  next airport header.

The parser is exercised against this fixture so the tests don't
depend on the user's X-Plane install.  A separate test module can
do integration-style checks against the real SPJC data later.
"""
import os

import pytest
from shapely.geometry import Polygon

from auto_patch import apt_dat_reader as APR


_FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures",
    "synthetic_apt.dat",
)


# ──────────────────────────────────────────────────────────────────────
# load_airport — top-level
# ──────────────────────────────────────────────────────────────────────
class TestLoadAirport:
    def test_loads_test_airport_one(self):
        apt = APR.load_airport(_FIXTURE, "ZZZZ")
        assert apt is not None
        assert apt.icao == "ZZZZ"
        assert apt.name.startswith("Test Airport One")
        assert apt.reference_elev_ft == 100
        assert apt.reference_elev_m == pytest.approx(30.48, abs=0.01)
        assert apt.source_path == _FIXTURE

    def test_loads_other_airport_independently(self):
        """The parser must stop at the next ``1`` header — YYYY's data
        must not bleed into ZZZZ and vice versa.
        """
        zzzz = APR.load_airport(_FIXTURE, "ZZZZ")
        yyyy = APR.load_airport(_FIXTURE, "YYYY")
        assert zzzz is not None and yyyy is not None
        assert len(zzzz.runways) == 1
        assert len(yyyy.runways) == 1
        assert zzzz.runways[0].desig_a == "09"
        assert yyyy.runways[0].desig_a == "18"
        assert len(yyyy.pavements) == 0    # YYYY has no row 110

    def test_unknown_icao_returns_none(self):
        assert APR.load_airport(_FIXTURE, "AAAA") is None

    def test_missing_file_returns_none(self):
        assert APR.load_airport("/nonexistent/path/apt.dat", "ZZZZ") is None

    def test_case_insensitive_icao(self):
        apt = APR.load_airport(_FIXTURE, "zzzz")
        assert apt is not None
        assert apt.icao == "ZZZZ"


# ──────────────────────────────────────────────────────────────────────
# Runway parsing
# ──────────────────────────────────────────────────────────────────────
class TestRunwayParsing:
    def test_runway_fields(self):
        apt = APR.load_airport(_FIXTURE, "ZZZZ")
        rwy = apt.runways[0]
        assert rwy.desig_a == "09"
        assert rwy.desig_b == "27"
        assert rwy.lat_a == pytest.approx(-12.0)
        assert rwy.lon_a == pytest.approx(-77.1)
        assert rwy.lat_b == pytest.approx(-12.0)
        assert rwy.lon_b == pytest.approx(-77.09)
        assert rwy.width_m == 45.0
        assert rwy.surface_code == 1
        assert rwy.displaced_a_m == 0.0
        assert rwy.displaced_b_m == 0.0

    def test_runway_blast_pad_fields(self):
        """Blast-pad lengths (end-block index 4, distinct from the
        displaced threshold at index 3) feed the runway elevation
        profile downstream, so parsing the right token matters."""
        apt = APR.load_airport(_FIXTURE, "ZZZZ")
        rwy = apt.runways[0]
        assert rwy.blast_a_m == 60.0
        assert rwy.blast_b_m == 60.0

    def test_runway_approach_metadata_defaults(self):
        """The synthetic fixture's end blocks carry zeros for markings
        and approach lights — the parsed fields mirror that."""
        apt = APR.load_airport(_FIXTURE, "ZZZZ")
        rwy = apt.runways[0]
        assert rwy.markings_a == 0
        assert rwy.markings_b == 0
        assert rwy.approach_lights_a == 0
        assert rwy.approach_lights_b == 0

    def test_runway_approach_metadata_fields(self):
        """Markings (end-block index 5) and approach lights (index 6)
        are retained per end: precision markings + ALSF-II on end a,
        visual markings + ODALS on end b."""
        row = ("100 45.00 1 0 0.25 1 1 0 "
               "09 -12.0 -77.1 0 60 3 2 1 1 "
               "27 -12.0 -77.09 0 60 1 11 0 0")
        rwy = APR._parse_runway(row.split())
        assert rwy is not None
        assert rwy.markings_a == 3
        assert rwy.approach_lights_a == 2
        assert rwy.markings_b == 1
        assert rwy.approach_lights_b == 11

    def test_runway_approach_metadata_bad_tokens_read_zero(self):
        """Non-numeric markings / approach-light tokens degrade to 0
        instead of rejecting the whole runway row."""
        row = ("100 45.00 1 0 0.25 1 1 0 "
               "09 -12.0 -77.1 0 60 x y 1 1 "
               "27 -12.0 -77.09 0 60 1 11 0 0")
        rwy = APR._parse_runway(row.split())
        assert rwy is not None
        assert rwy.markings_a == 0
        assert rwy.approach_lights_a == 0
        assert rwy.markings_b == 1
        assert rwy.approach_lights_b == 11


# ──────────────────────────────────────────────────────────────────────
# Pavement parsing
# ──────────────────────────────────────────────────────────────────────
class TestPavementParsing:
    def test_three_pavements_emitted(self):
        apt = APR.load_airport(_FIXTURE, "ZZZZ")
        assert len(apt.pavements) == 3

    def test_pavement_names_preserved(self):
        apt = APR.load_airport(_FIXTURE, "ZZZZ")
        names = [p.name for p in apt.pavements]
        assert "SQUARE" in names
        assert "BEZIER_RECT" in names
        assert "NESTED_HOLE" in names

    def test_simple_square_polygon(self):
        apt = APR.load_airport(_FIXTURE, "ZZZZ")
        pav = next(p for p in apt.pavements if p.name == "SQUARE")
        # No Bezier curves → exactly 4 unique vertices in the
        # exterior ring (5 with the closing repeat).
        coords = list(pav.polygon.exterior.coords)
        assert len(coords) == 5
        assert coords[0] == coords[-1]
        # The polygon is small but should have non-zero area.
        assert pav.polygon.area > 0
        assert pav.surface_code == 1

    def test_pavement_vertices_are_lon_lat_ordered(self):
        """Node rows are ``111 lat lon`` but shapely needs (x, y) =
        (lon, lat).  Verify the SQUARE polygon's coordinates carry
        longitude in x and latitude in y — a lon/lat swap would
        silently transpose every airport's geometry."""
        apt = APR.load_airport(_FIXTURE, "ZZZZ")
        pav = next(p for p in apt.pavements if p.name == "SQUARE")
        minx, miny, maxx, maxy = pav.polygon.bounds
        # x = longitude ∈ [-77.101, -77.099]; y = latitude ∈ [-12.001, -11.999].
        assert minx == pytest.approx(-77.101)
        assert maxx == pytest.approx(-77.099)
        assert miny == pytest.approx(-12.001)
        assert maxy == pytest.approx(-11.999)

    def test_bezier_pavement_has_extra_vertices(self):
        apt = APR.load_airport(_FIXTURE, "ZZZZ")
        pav = next(p for p in apt.pavements if p.name == "BEZIER_RECT")
        coords = list(pav.polygon.exterior.coords)
        # 4 base nodes + Bezier subdivision means more than 5 total.
        assert len(coords) > 5
        assert pav.polygon.is_valid
        assert pav.polygon.area > 0
        assert pav.surface_code == 2

    def test_nested_hole_polygon(self):
        apt = APR.load_airport(_FIXTURE, "ZZZZ")
        pav = next(p for p in apt.pavements if p.name == "NESTED_HOLE")
        # Outer ring + 1 interior ring (the hole).
        assert len(pav.polygon.interiors) == 1
        # The hole reduces total area but doesn't make the polygon empty.
        assert pav.polygon.area > 0
        assert pav.polygon.is_valid

    def test_split_handle_zero_length_span_no_spike(self):
        # WED encodes a SPLIT bezier handle as a RUN of same-anchor nodes
        # (a zero-length span).  ``_interpolate_contour`` must SKIP the
        # degenerate span, not tessellate a self-intersecting spike across
        # it — otherwise ``_parse_pavement``'s buffer(0) repair punches a
        # spurious HOLE (losing real pavement, e.g. HECA taxiway fillets).
        # A unit square whose top-right corner carries a split handle
        # (two same-anchor 112 nodes, outgoing + incoming).
        rows = [
            ["111", "0.0000", "0.0000"],
            ["111", "0.0000", "0.0010"],
            ["112", "0.0010", "0.0010", "0.0010", "0.0014"],   # out handle
            ["112", "0.0010", "0.0010", "0.0010", "0.0006"],   # in handle (split)
            ["113", "0.0010", "0.0000"],
        ]
        ring = APR._interpolate_contour(rows, APR.DEFAULT_BEZIER_SEGMENTS)
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        poly = Polygon(ring)
        assert poly.is_valid          # no self-intersecting spike
        assert poly.area > 0
        assert len(poly.interiors) == 0   # no spurious hole punched


# ──────────────────────────────────────────────────────────────────────
# Boundary parsing
# ──────────────────────────────────────────────────────────────────────
class TestBoundaryParsing:
    def test_boundary_polygon_present(self):
        apt = APR.load_airport(_FIXTURE, "ZZZZ")
        assert apt.boundary is not None
        assert isinstance(apt.boundary, Polygon)
        assert apt.boundary.area > 0


# ──────────────────────────────────────────────────────────────────────
# Bezier interpolation primitives
# ──────────────────────────────────────────────────────────────────────
class TestBezierInterpolation:
    def test_quadratic_bezier_endpoints(self):
        pts = APR._quadratic_bezier((0, 0), (1, 1), (2, 0), n_segments=4)
        assert len(pts) == 5
        assert pts[0] == (0, 0)
        assert pts[-1] == (2, 0)

    def test_quadratic_bezier_midpoint(self):
        # Quadratic Bezier midpoint is at (P0 + 2*P1 + P2) / 4 for t=0.5
        pts = APR._quadratic_bezier((0, 0), (10, 10), (20, 0), n_segments=2)
        mid = pts[1]
        assert mid[0] == pytest.approx(10.0)
        assert mid[1] == pytest.approx(5.0)

    def test_cubic_bezier_endpoints(self):
        pts = APR._cubic_bezier((0, 0), (1, 1), (2, 1), (3, 0),
                                n_segments=8)
        assert len(pts) == 9
        assert pts[0] == (0, 0)
        assert pts[-1] == (3, 0)

    def test_mirror_through_anchor(self):
        assert APR._mirror((1, 2), (0, 0)) == (-1, -2)
        assert APR._mirror((5, 5), (3, 3)) == (1, 1)


# ──────────────────────────────────────────────────────────────────────
# find_airport_apt_dat — search priority
# ──────────────────────────────────────────────────────────────────────
class TestFindAirportAptDat:
    def test_returns_none_when_root_missing(self):
        assert APR.find_airport_apt_dat("/nonexistent", "SPJC") is None

    def test_returns_none_when_icao_missing(self):
        assert APR.find_airport_apt_dat("/tmp", "") is None

    def test_finds_per_airport_pack_first(self, tmp_path):
        """A per-airport Custom Scenery pack should win over the
        Global Airports pack.  Build a fake X-Plane root with both.
        """
        xp = tmp_path / "X-Plane 12"
        cs = xp / "Custom Scenery"

        # 1) Per-airport pack containing ZZZZ
        per_apt_pack = cs / "ZZZZ Per Airport Pack" / "Earth nav data"
        per_apt_pack.mkdir(parents=True)
        per_apt_path = per_apt_pack / "apt.dat"
        per_apt_path.write_text(
            "A\n"
            "1    100 0 0 ZZZZ Per-Airport Test\n"
            "100 45.00 1 0 0.25 1 1 0 09 -12 -77 0 60 0 0 0 0 27 -12 -77.01 0 60 0 0 0 0\n",
            encoding="utf-8")

        # 2) Global Airports pack also containing ZZZZ
        global_pack = cs / "Global Airports" / "Earth nav data"
        global_pack.mkdir(parents=True)
        global_path = global_pack / "apt.dat"
        global_path.write_text(
            "A\n"
            "1    100 0 0 ZZZZ Global Test\n"
            "100 45.00 1 0 0.25 1 1 0 09 -12 -77 0 60 0 0 0 0 27 -12 -77.01 0 60 0 0 0 0\n",
            encoding="utf-8")

        found = APR.find_airport_apt_dat(str(xp), "ZZZZ")
        assert found == str(per_apt_path)

    def test_falls_back_to_global(self, tmp_path):
        """If no per-airport pack contains the ICAO, fall back to the
        Global Airports pack.
        """
        xp = tmp_path / "X-Plane 12"
        cs = xp / "Custom Scenery"

        # Per-airport pack contains a DIFFERENT airport
        other_pack = cs / "Other Pack" / "Earth nav data"
        other_pack.mkdir(parents=True)
        (other_pack / "apt.dat").write_text(
            "A\n"
            "1    100 0 0 AAAA Other Airport\n",
            encoding="utf-8")

        # Global has ZZZZ
        global_pack = cs / "Global Airports" / "Earth nav data"
        global_pack.mkdir(parents=True)
        global_path = global_pack / "apt.dat"
        global_path.write_text(
            "A\n"
            "1    100 0 0 ZZZZ Global Test\n",
            encoding="utf-8")

        found = APR.find_airport_apt_dat(str(xp), "ZZZZ")
        assert found == str(global_path)

    def test_falls_back_to_default(self, tmp_path):
        """If neither Custom Scenery contains the ICAO, try the
        default scenery's apt.dat.
        """
        xp = tmp_path / "X-Plane 12"
        default_pack = (xp / "Resources" / "default scenery"
                        / "default apt dat" / "Earth nav data")
        default_pack.mkdir(parents=True)
        default_path = default_pack / "apt.dat"
        default_path.write_text(
            "A\n"
            "1    100 0 0 ZZZZ Default Test\n",
            encoding="utf-8")

        # Custom Scenery dir doesn't even exist
        found = APR.find_airport_apt_dat(str(xp), "ZZZZ")
        assert found == str(default_path)

    def test_returns_none_when_icao_nowhere(self, tmp_path):
        xp = tmp_path / "X-Plane 12"
        (xp / "Custom Scenery").mkdir(parents=True)
        assert APR.find_airport_apt_dat(str(xp), "ZZZZ") is None


# ──────────────────────────────────────────────────────────────────────
# Ramp starts (rows 1300/1301) and ground-vehicle routes (row 1206)
# ──────────────────────────────────────────────────────────────────────
# Real-format rows modeled on CYXY Whitehorse (our reference airport):
# gate ramp starts with ICAO size codes, oneway fuel-truck service roads.
_RAMP_TRUCK_BLOCK = (
    "A\n"
    "1    700 0 0 ZRMP Ramp Test\n"
    "100 45.00 1 0 0.25 1 1 0 09 60.7100000 -135.0800000 0 60 0 0 0 0"
    " 27 60.7100000 -135.0700000 0 60 0 0 0 0\n"
    # Two gate ramp starts, each followed by its 1301 metadata.
    "1300 60.71398647 -135.07523025 -52.5 gate jets Gate 1\n"
    "1301 C airline \n"
    "1300 60.71426307 -135.07493699 -88.0 gate turboprops|props Gate 2\n"
    "1301 B airline baw afr\n"
    # A 1300 with NO following 1301 (size_code must stay empty).
    "1300 60.71459517 -135.07548657 12.0 tie_down props GA Tie 3\n"
    # Shared 1201 routing nodes used by both taxi (1202) and truck (1206).
    "1201 60.7140000 -135.0750000 both 0 node0\n"
    "1201 60.7142000 -135.0752000 both 1 node1\n"
    "1201 60.7144000 -135.0754000 both 2 node2\n"
    # One aircraft taxi edge (must NOT land in truck_edges).
    "1202 0 1 twoway taxiway_C A\n"
    # Two oneway ground-vehicle (fuel truck) route edges, named.
    "1206 0 1 oneway Terminal fuel truck\n"
    "1206 1 2 oneway Terminal fuel truck\n"
)


def _load_ramp_truck(tmp_path):
    p = tmp_path / "apt.dat"
    p.write_text(_RAMP_TRUCK_BLOCK, encoding="utf-8")
    return APR.load_airport(str(p), "ZRMP")


class TestTruckEdgeParsing:
    def test_truck_edges_separate_from_taxi(self, tmp_path):
        apt = _load_ramp_truck(tmp_path)
        assert len(apt.truck_edges) == 2
        # The single 1202 aircraft edge stays in taxi_edges, not trucks.
        assert len(apt.taxi_edges) == 1

    def test_truck_edge_fields(self, tmp_path):
        apt = _load_ramp_truck(tmp_path)
        te = apt.truck_edges[0]
        assert te.node_from == 0 and te.node_to == 1
        assert te.direction == "oneway"
        assert te.kind == "truck"
        assert te.name == "Terminal fuel truck"

    def test_service_road_centerlines(self, tmp_path):
        apt = _load_ramp_truck(tmp_path)
        # Identity meter projection (lon, lat) -> (x, y) scaled up so the
        # two ~consecutive segments are well over the 0.1 m collapse floor.
        def to_m(lon, lat):
            return (lon * 1e5, lat * 1e5)
        cls = APR.service_road_centerlines(apt, to_m)
        assert len(cls) == 1                 # both edges share one name
        tcl = cls[0]
        assert tcl.name == "Terminal fuel truck"
        # Ground-vehicle route: marked OUT of the aircraft taxi spine.
        assert tcl.is_service
        # Merged across the two edges -> 3 vertices (nodes 0,1,2).
        assert len(tcl.line.coords) == 3
        # No ICAO taxi size on a truck route: one empty letter per segment.
        assert tcl.seg_sizes == ["", ""]

    def test_service_road_centerlines_empty_without_trucks(self, tmp_path):
        # Reuse the runway-only ZZZZ-style block: no 1206 rows.
        p = tmp_path / "apt2.dat"
        p.write_text(
            "A\n"
            "1    100 0 0 ZNOP No Trucks\n"
            "100 45.00 1 0 0.25 1 1 0 09 -12 -77 0 60 0 0 0 0"
            " 27 -12 -77.01 0 60 0 0 0 0\n",
            encoding="utf-8")
        apt = APR.load_airport(str(p), "ZNOP")
        assert apt.truck_edges == []
        assert APR.service_road_centerlines(apt, lambda lon, lat: (lon, lat)) == []


class TestPaintedLines:
    """Row-120 painted linear features (taxiway centerlines etc.)."""

    def _load(self, tmp_path):
        p = tmp_path / "apt_lines.dat"
        p.write_text(
            "A\n"
            "1    100 0 0 ZPNT Painted Lines\n"
            "100 45.00 1 0 0.25 1 1 0 09 -12.000 -77.000 0 60 0 0 0 0"
            " 27 -12.000 -77.010 0 60 0 0 0 0\n"
            # Open centerline (paint 1), one bezier node, 115 end.
            "120 CL A\n"
            "111 -12.0010 -77.0010 1\n"
            "112 -12.0020 -77.0020 -12.0030 -77.0020 1\n"
            "115 -12.0040 -77.0030\n"
            # Open hold bar (paint 4) — excluded by the classifier.
            "120 hold\n"
            "111 -12.0050 -77.0040 4\n"
            "115 -12.0051 -77.0041\n"
            # Closed loop (paint 1) — excluded by the classifier.
            "120 loop\n"
            "111 -12.0060 -77.0050 1\n"
            "111 -12.0060 -77.0060 1\n"
            "111 -12.0070 -77.0060 1\n"
            "113 -12.0070 -77.0050 1\n",
            encoding="utf-8")
        return APR.load_airport(str(p), "ZPNT")

    def test_parses_all_blocks(self, tmp_path):
        apt = self._load(tmp_path)
        assert len(apt.painted_lines) == 3
        open_cl, hold, loop = apt.painted_lines
        assert open_cl.paint_codes == frozenset({1})
        assert not open_cl.closed
        # The bezier node tessellates: more vertices than the 3 nodes.
        assert len(open_cl.line.coords) > 3
        assert hold.paint_codes == frozenset({4})
        assert loop.closed
        # Closed ring repeats the first vertex.
        c = list(loop.line.coords)
        assert c[0] == c[-1]

    def test_is_centerline_paint(self, tmp_path):
        apt = self._load(tmp_path)
        open_cl, hold, loop = apt.painted_lines
        assert open_cl.is_centerline_paint
        assert not hold.is_centerline_paint
        assert loop.is_centerline_paint  # paint says yes ...

    def test_painted_taxi_centerlines_filters(self, tmp_path):
        apt = self._load(tmp_path)

        def to_m(lon, lat):
            return ((lon + 77.0) * 1e5, (lat + 12.0) * 1e5)
        # No pavement/runway gates: only the paint-code + closed
        # filters apply -> exactly the open centerline survives.
        out = APR.painted_taxi_centerlines(apt, to_m)
        assert len(out) == 1
        tcl = out[0]
        assert tcl.name == "P1"
        assert tcl.line.length > 8.0
        # Painted lines carry no apt.dat ICAO size (default taxi cap) and are
        # aircraft centerlines, not ground-vehicle routes.
        assert not tcl.is_service
        assert tcl.seg_sizes == [""] * (len(tcl.line.coords) - 1)

    def test_no_painted_lines_block(self, tmp_path):
        p = tmp_path / "apt_plain.dat"
        p.write_text(
            "A\n"
            "1    100 0 0 ZNOL No Lines\n"
            "100 45.00 1 0 0.25 1 1 0 09 -12 -77 0 60 0 0 0 0"
            " 27 -12 -77.01 0 60 0 0 0 0\n",
            encoding="utf-8")
        apt = APR.load_airport(str(p), "ZNOL")
        assert apt.painted_lines == []
        assert APR.painted_taxi_centerlines(
            apt, lambda lon, lat: (lon, lat)) == []
