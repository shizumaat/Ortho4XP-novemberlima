"""DSF vector road-network reader — grammar, level flags, tolerance.

Fixtures are minimal synthetic DSFTool-dump snippets written by the
tests themselves (no third-party pack content enters the repository).
They are shaped like the verified ``US-KBNA Nashville Roads`` grammar:
one ``NETWORK_DEF`` table, ``BEGIN_SEGMENT`` / ``SHAPE_POINT`` /
``END_SEGMENT`` with the per-vertex draping level flag (0 = draped,
1+ = elevated).

A smoke test runs against the real KBNA Roads dump IF it is present in
the session scratchpad; it is skipped otherwise so the suite never
depends on pack data.
"""
import os

import pytest

from auto_patch.dsf_road_network import (
    LEVEL_DRAPED_MAX_ABS,
    RoadNetwork,
    parse_dsf_road_networks,
    read_dsf_road_networks,
    segments_crossing,
)


def _dump(*body_lines: str) -> list[str]:
    """A minimal well-formed dump header plus the given body lines."""
    return [
        "A",
        "800 written by DSFTool 2.4.0-b1",
        "DSF2TEXT",
        "HEIGHTS 0.50000 0.0",
        "PROPERTY sim/overlay 1",
        *body_lines,
    ]


class TestGrammar:
    def test_single_draped_segment(self):
        network = parse_dsf_road_networks(_dump(
            "NETWORK_DEF lib/g10/roads_EU.net",
            "BEGIN_SEGMENT 0 20 494 -86.6784 36.1060 -0.000000000",
            "SHAPE_POINT -86.6791 36.1053 -0.000000000",
            "SHAPE_POINT -86.6799 36.1042 -0.000000000",
            "END_SEGMENT 495 -86.6800 36.1040 -0.000000000",
        ))
        assert network.network_definition == "lib/g10/roads_EU.net"
        assert network.network_definitions == ["lib/g10/roads_EU.net"]
        assert network.skipped_line_count == 0
        assert len(network.segments) == 1
        segment = network.segments[0]
        assert segment.road_subtype == 20
        assert segment.network_definition_index == 0
        assert segment.network_definition_path == "lib/g10/roads_EU.net"
        assert segment.start_junction_id == 494
        assert segment.end_junction_id == 495
        # BEGIN node + two SHAPE_POINTs + END node = four vertices.
        assert len(segment.shape_points) == 4
        assert segment.shape_points[0].longitude == pytest.approx(-86.6784)
        assert segment.shape_points[0].latitude == pytest.approx(36.1060)
        assert segment.shape_points[-1].longitude == pytest.approx(-86.6800)
        assert segment.is_fully_draped
        assert all(point.draped for point in segment.shape_points)

    def test_level_zero_versus_level_one_points(self):
        """The decisive feature-B signal: level 0 decodes draped, 1+
        decodes elevated, per point, on a single segment."""
        network = parse_dsf_road_networks(_dump(
            "NETWORK_DEF lib/g10/roads_EU.net",
            "BEGIN_SEGMENT 0 60 411 -86.6714 36.1097 -0.000000000",
            "SHAPE_POINT -86.6713 36.1104 1.000000000",
            "SHAPE_POINT -86.6707 36.1128 1.000000000",
            "SHAPE_POINT -86.6707 36.1130 -0.000000000",
            "END_SEGMENT 412 -86.6704 36.1141 2.000000000",
        ))
        segment = network.segments[0]
        levels = [point.level for point in segment.shape_points]
        assert levels == pytest.approx([0.0, 1.0, 1.0, 0.0, 2.0])
        draped = [point.draped for point in segment.shape_points]
        assert draped == [True, False, False, True, False]
        assert not segment.is_fully_draped
        assert segment.road_subtype == 60

    def test_draped_threshold_is_half_unit(self):
        """HEIGHTS quantises to half units; the cut sits at 0.5."""
        assert LEVEL_DRAPED_MAX_ABS == 0.5
        network = parse_dsf_road_networks(_dump(
            "NETWORK_DEF a.net",
            "BEGIN_SEGMENT 0 10 1 0.0 0.0 -0.000000000",
            "END_SEGMENT 2 0.001 0.001 1.000000000",
        ))
        points = network.segments[0].shape_points
        assert points[0].draped is True
        assert points[1].draped is False

    def test_multiple_network_definitions(self):
        """The format permits several NETWORK_DEFs; a segment's first
        integer indexes the table and resolves to the right .net path."""
        network = parse_dsf_road_networks(_dump(
            "NETWORK_DEF lib/g10/roads_EU.net",
            "NETWORK_DEF lib/g10/roads_US.net",
            "BEGIN_SEGMENT 0 20 1 0.0 0.0 -0.0",
            "END_SEGMENT 2 0.1 0.1 -0.0",
            "BEGIN_SEGMENT 1 30 3 1.0 1.0 -0.0",
            "END_SEGMENT 4 1.1 1.1 -0.0",
        ))
        assert network.network_definitions == [
            "lib/g10/roads_EU.net",
            "lib/g10/roads_US.net",
        ]
        # network_definition (singular) returns the first when several.
        assert network.network_definition == "lib/g10/roads_EU.net"
        assert len(network.segments) == 2
        assert network.segments[0].network_definition_path == \
            "lib/g10/roads_EU.net"
        assert network.segments[1].network_definition_index == 1
        assert network.segments[1].network_definition_path == \
            "lib/g10/roads_US.net"

    def test_segment_without_shape_points(self):
        """A BEGIN immediately followed by END is a two-vertex segment."""
        network = parse_dsf_road_networks(_dump(
            "NETWORK_DEF a.net",
            "BEGIN_SEGMENT 0 50 7 -1.0 2.0 -0.0",
            "END_SEGMENT 8 -1.1 2.1 -0.0",
        ))
        assert len(network.segments[0].shape_points) == 2
        assert network.skipped_line_count == 0

    def test_out_of_range_definition_index_resolves_to_empty(self):
        network = parse_dsf_road_networks(_dump(
            "NETWORK_DEF a.net",
            "BEGIN_SEGMENT 5 20 1 0.0 0.0 -0.0",
            "END_SEGMENT 2 0.1 0.1 -0.0",
        ))
        assert network.segments[0].network_definition_path == ""
        assert network.segments[0].network_definition_index == 5


class TestMalformedTolerance:
    def test_unrecognised_lines_ignored_silently(self):
        network = parse_dsf_road_networks(_dump(
            "NETWORK_DEF a.net",
            "PROPERTY sim/exclude_net -86.6/36.1/-86.5/36.2;-86.6/36.1,-86.5/36.2",
            "# a comment",
            "BEGIN_SEGMENT 0 20 1 0.0 0.0 -0.0",
            "END_SEGMENT 2 0.1 0.1 -0.0",
        ))
        assert len(network.segments) == 1
        assert network.skipped_line_count == 0

    def test_bad_shape_point_skipped_and_counted(self):
        network = parse_dsf_road_networks(_dump(
            "NETWORK_DEF a.net",
            "BEGIN_SEGMENT 0 20 1 0.0 0.0 -0.0",
            "SHAPE_POINT -86.6 not_a_number -0.0",
            "SHAPE_POINT -86.5 36.2 -0.0",
            "END_SEGMENT 2 0.1 0.1 -0.0",
        ))
        assert network.skipped_line_count == 1
        # The good shape point survives: BEGIN + one SHAPE + END.
        assert len(network.segments[0].shape_points) == 3

    def test_orphan_shape_point_and_end_counted(self):
        network = parse_dsf_road_networks(_dump(
            "NETWORK_DEF a.net",
            "SHAPE_POINT -86.6 36.1 -0.0",
            "END_SEGMENT 2 0.1 0.1 -0.0",
        ))
        assert network.segments == []
        assert network.skipped_line_count == 2

    def test_unterminated_segment_at_next_begin_counted(self):
        network = parse_dsf_road_networks(_dump(
            "NETWORK_DEF a.net",
            "BEGIN_SEGMENT 0 20 1 0.0 0.0 -0.0",
            "SHAPE_POINT -86.6 36.1 -0.0",
            "BEGIN_SEGMENT 0 30 3 1.0 1.0 -0.0",
            "END_SEGMENT 4 1.1 1.1 -0.0",
        ))
        # First segment dropped (never terminated); second survives.
        assert len(network.segments) == 1
        assert network.segments[0].road_subtype == 30
        assert network.skipped_line_count == 1

    def test_unterminated_segment_at_end_of_file_counted(self):
        network = parse_dsf_road_networks(_dump(
            "NETWORK_DEF a.net",
            "BEGIN_SEGMENT 0 20 1 0.0 0.0 -0.0",
            "SHAPE_POINT -86.6 36.1 -0.0",
        ))
        assert network.segments == []
        assert network.skipped_line_count == 1

    def test_network_definition_without_path_skipped_and_counted(self):
        """A bare NETWORK_DEF carries no library path: it must be
        counted as malformed, not crash, and not enter the table."""
        network = parse_dsf_road_networks(_dump(
            "NETWORK_DEF",
            "NETWORK_DEF   ",
            "NETWORK_DEF a.net",
            "BEGIN_SEGMENT 0 20 1 0.0 0.0 -0.0",
            "END_SEGMENT 2 0.1 0.1 -0.0",
        ))
        assert network.network_definitions == ["a.net"]
        assert network.skipped_line_count == 2
        assert len(network.segments) == 1

    def test_short_begin_segment_skipped(self):
        network = parse_dsf_road_networks(_dump(
            "NETWORK_DEF a.net",
            "BEGIN_SEGMENT 0 20 1 0.0",
            "END_SEGMENT 2 0.1 0.1 -0.0",
        ))
        # Bad BEGIN skipped; the following END has no open segment.
        assert network.segments == []
        assert network.skipped_line_count == 2


class TestSpatialFilter:
    def test_segments_crossing_polygon(self):
        network = parse_dsf_road_networks(_dump(
            "NETWORK_DEF a.net",
            "BEGIN_SEGMENT 0 20 1 0.0 0.0 -0.0",
            "END_SEGMENT 2 1.0 1.0 -0.0",
            "BEGIN_SEGMENT 0 20 3 10.0 10.0 -0.0",
            "END_SEGMENT 4 11.0 11.0 -0.0",
        ))
        box = [(-0.5, -0.5), (2.0, -0.5), (2.0, 2.0), (-0.5, 2.0)]
        hits = segments_crossing(network, box)
        assert len(hits) == 1
        assert hits[0].start_junction_id == 1


class TestFileReader:
    def test_read_from_path(self, tmp_path):
        path = tmp_path / "roads.txt"
        path.write_text("\n".join(_dump(
            "NETWORK_DEF lib/g10/roads_EU.net",
            "BEGIN_SEGMENT 0 20 1 -86.6 36.1 -0.0",
            "END_SEGMENT 2 -86.5 36.2 -0.0",
        )) + "\n")
        network = read_dsf_road_networks(str(path))
        assert network.network_definition == "lib/g10/roads_EU.net"
        assert len(network.segments) == 1


# Real-pack smoke test: runs only when a KBNA Roads dump exists (never
# committed). Point KBNA_ROADS_DSF_DUMP at a DSFTool text dump of
# "US-KBNA Nashville Roads/Earth nav data/+30-090/+36-087.dsf" to run it;
# the default is the dump location of the session that authored this test.
_KBNA_ROADS_DUMP = os.environ.get(
    "KBNA_ROADS_DSF_DUMP",
    "/private/tmp/claude-501/-Users-noah-Ortho4XP-novemberlima/"
    "3f95dd9d-7e39-4a51-971d-478d7d47f51d/scratchpad/kbna_roads.txt",
)


@pytest.mark.skipif(
    not os.path.isfile(_KBNA_ROADS_DUMP),
    reason="KBNA Nashville Roads dump not present in scratchpad",
)
class TestKbnaSmoke:
    def test_kbna_road_network_shape(self):
        network = read_dsf_road_networks(_KBNA_ROADS_DUMP)
        assert network.network_definition == "lib/g10/roads_EU.net"
        assert len(network.segments) == 627
        assert network.skipped_line_count == 0

    def test_donelson_pike_draped_subtype_20(self):
        """The road draped under the taxiway-L bridge (near longitude
        -86.666, latitude 36.1226) is subtype 20 with level-0 points."""
        network = read_dsf_road_networks(_KBNA_ROADS_DUMP)
        target_longitude, target_latitude = -86.666, 36.1226
        matched_subtype_20_draped = False
        for segment in network.segments:
            for point in segment.shape_points:
                near = (
                    abs(point.longitude - target_longitude) < 0.003
                    and abs(point.latitude - target_latitude) < 0.003
                )
                if near and segment.road_subtype == 20 and point.draped:
                    matched_subtype_20_draped = True
        assert matched_subtype_20_draped
