"""Tests for the implied-tunnel level-crossing feature (user 2026-07-16).

Three landed behaviours are exercised here, all headless (no network,
no X-Plane install, ``tmp_path`` only):

A. ``bridges._carriageway_width_from_tags`` — carriageway width derived
   from a road way's own OSM ``width=`` / ``lanes=`` measurements, with
   sanity clamps and a fall-through to the per-highway-type table.

B. The at-grade level-crossing VETO inside
   ``bridges._synthesize_implied_crossing_bores`` — positive OSM
   evidence (an ``aeroway=aircraft_crossing`` node, or a nearby
   ``barrier`` gate) on a public road that crosses runway/taxiway
   pavement keeps the road at grade instead of synthesising a bore.

C. The OSM cache node-tag round-trip — ``OSM_layer.update_dicosm`` /
   ``write_to_file`` / ``_cached_osm_schema_matches`` /
   ``osm_load._load_osm_tile`` carry the whitelisted node tags (and the
   schema marker) through a write-then-read cycle.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from shapely.geometry import box

# Mirror the established test idiom (see tests/test_boundary.py): put the
# project's ``src/`` on sys.path so the O4 / auto_patch modules import
# directly without installing the package.  ``conftest.py`` also does this,
# but the explicit insert keeps the module importable in isolation.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import O4_OSM_Utils as OSM  # noqa: E402
from auto_patch import bridges  # noqa: E402
from auto_patch import osm_load  # noqa: E402
from auto_patch.layout import BuiltShape  # noqa: E402


# ──────────────────────────────────────────────────────────────────
# A. Carriageway width from OSM tags
# ──────────────────────────────────────────────────────────────────
class TestCarriagewayWidthFromTags:
    """``_carriageway_width_from_tags`` prefers a way's own ``width=`` /
    ``lanes=`` measurements over the per-type table, with sanity clamps.

    ``primary`` sits at 18.0 m in ``HIGHWAY_CARRIAGEWAY_WIDTH_M``, so
    every table fall-through below resolves to 18.0 (the ``default_m``
    argument is only consulted for highway types absent from the table).
    """

    _PRIMARY_TABLE_WIDTH = 18.0

    @pytest.mark.parametrize(
        "highway_type, tags, default_m, expected_width",
        [
            # width= wins, plain metres.
            ("primary", {"width": "6.5"}, 22.0, 6.5),
            # width= with a comma decimal and a trailing unit.
            ("primary", {"width": "6,5 m"}, 22.0, 6.5),
            # width below the [2.5, 40] clamp → table fall-through.
            ("primary", {"width": "0.5"}, 22.0, _PRIMARY_TABLE_WIDTH),
            # unparseable width → table fall-through.
            ("primary", {"width": "garbage"}, 22.0, _PRIMARY_TABLE_WIDTH),
            # lanes= × LANE_WIDTH_M (3.5).
            ("primary", {"lanes": "2"}, 22.0, 7.0),
            # single lane clamps up to the 4.0 m floor.
            ("primary", {"lanes": "1"}, 22.0, 4.0),
            # lanes above the [1, 10] clamp → table fall-through.
            ("primary", {"lanes": "12"}, 22.0, _PRIMARY_TABLE_WIDTH),
            # width beats lanes when both are present.
            ("primary", {"width": "6.5", "lanes": "2"}, 22.0, 6.5),
            # tags=None → table.
            ("primary", None, 22.0, _PRIMARY_TABLE_WIDTH),
            # empty tags, primary type → table value 18.0.
            ("primary", {}, 22.0, _PRIMARY_TABLE_WIDTH),
        ],
    )
    def test_width_resolution(
        self,
        highway_type: str,
        tags: dict | None,
        default_m: float,
        expected_width: float,
    ) -> None:
        """Each (tags, type) combination resolves to the documented width."""
        result = bridges._carriageway_width_from_tags(
            highway_type, tags, default_m)
        assert result == pytest.approx(expected_width)


# ──────────────────────────────────────────────────────────────────
# B. Implied-bore level-crossing veto
# ──────────────────────────────────────────────────────────────────
def _make_crossing_scene() -> tuple:
    """Build the synthetic runway-crossing scene in LOCAL METRES.

    Returns ``(layout, nodes_m, road_node_ids, road_way)`` where:

      * ``layout`` carries a single 1000×50 m runway strip
        (``box(0, -25, 1000, 25)``, role ``runway``);
      * ``road_way`` is a ``highway=primary`` way running perpendicular
        across the middle of the strip at ``x = 500`` from
        ``y = -300`` to ``y = 300``.

    The crossing segment (``y ∈ [-25, 25]``) is 50 m long — comfortably
    inside the implied-bore length window — so with no level-crossing
    evidence exactly one synthetic bore piece is produced.
    """
    runway = BuiltShape(polygon=box(0.0, -25.0, 1000.0, 25.0),
                        role="runway")
    layout = SimpleNamespace(shapes=[runway])
    nodes_m = {
        "n1": (500.0, -300.0),
        "n2": (500.0, -100.0),
        "n3": (500.0, 0.0),
        "n4": (500.0, 100.0),
        "n5": (500.0, 300.0),
    }
    road_node_ids = ["n1", "n2", "n3", "n4", "n5"]
    road_way = ("road1", road_node_ids, {"highway": "primary"})
    return layout, nodes_m, road_node_ids, road_way


def _bore_pieces(ways: list) -> list:
    """Return the returned way pieces tagged as an implied tunnel bore."""
    return [w for w in ways if "o4_implied_tunnel" in w[2]]


class TestImpliedBoreLevelCrossingVeto:
    """A public through-road crossing runway pavement is normally split
    into an implied ``tunnel=yes`` bore; positive at-grade OSM evidence
    within the veto radii keeps it at grade instead."""

    def test_baseline_synthesizes_one_bore(self) -> None:
        """No node tags → exactly one synthetic bore piece (sanity)."""
        layout, nodes_m, _ids, road_way = _make_crossing_scene()
        ways, _gaps = bridges._synthesize_implied_crossing_bores(
            layout, dict(nodes_m), [road_way], None,
            low_connector_max_gap_m=0.0, node_tags=None)
        assert len(_bore_pieces(ways)) == 1

    def test_aircraft_crossing_node_vetoes_bore(self) -> None:
        """An ``aeroway=aircraft_crossing`` node ON the crossing vetoes
        the bore; the original way survives unsplit."""
        layout, nodes_m, road_node_ids, road_way = _make_crossing_scene()
        node_tags = {
            "n3": {"aeroway": "aircraft_crossing",
                   "crossing:aircraft": "ground"},
        }
        ways, _gaps = bridges._synthesize_implied_crossing_bores(
            layout, dict(nodes_m), [road_way], None,
            low_connector_max_gap_m=0.0, node_tags=node_tags)
        assert len(_bore_pieces(ways)) == 0
        # The original way is preserved intact (id + node refs unchanged).
        assert any(w[0] == "road1" and w[1] == road_node_ids
                   for w in ways)

    def test_nearby_gate_barrier_vetoes_bore(self) -> None:
        """A ``barrier=lift_gate`` node 75 m from the crossing segment
        (within the 120 m gate radius) vetoes the bore."""
        layout, nodes_m, _ids, road_way = _make_crossing_scene()
        # n2 at (500, -100) is 75 m from the pavement edge at y=-25.
        node_tags = {"n2": {"barrier": "lift_gate"}}
        ways, _gaps = bridges._synthesize_implied_crossing_bores(
            layout, dict(nodes_m), [road_way], None,
            low_connector_max_gap_m=0.0, node_tags=node_tags)
        assert len(_bore_pieces(ways)) == 0

    def test_distant_gate_barrier_does_not_veto(self) -> None:
        """A gate ~275 m from the crossing segment is too far to veto —
        the bore is still synthesised."""
        layout, nodes_m, _ids, road_way = _make_crossing_scene()
        # n1 at (500, -300) is ~275 m from the pavement edge at y=-25.
        node_tags = {"n1": {"barrier": "gate"}}
        ways, _gaps = bridges._synthesize_implied_crossing_bores(
            layout, dict(nodes_m), [road_way], None,
            low_connector_max_gap_m=0.0, node_tags=node_tags)
        assert len(_bore_pieces(ways)) == 1

    def test_mapped_tunnel_is_never_vetoed(self) -> None:
        """A mapped ``tunnel=yes`` way is re-split from our pavement even
        with aircraft-crossing evidence present — mapped tunnels are
        never vetoed.

        The re-split path is gated by ``O4_TUNNEL_TAXI_BREAKS`` (default
        "1").  A re-split bore piece carries both ``tunnel="yes"`` and
        ``o4_implied_tunnel="1"`` (the ``_is_bore`` branch tags them the
        same way regardless of the mapped origin), so exactly one such
        piece is produced for this geometry.
        """
        layout, nodes_m, _ids, _road_way = _make_crossing_scene()
        mapped_way = ("road1", _ids := ["n1", "n2", "n3", "n4", "n5"],
                      {"highway": "primary", "tunnel": "yes"})
        node_tags = {
            "n3": {"aeroway": "aircraft_crossing",
                   "crossing:aircraft": "ground"},
        }
        ways, _gaps = bridges._synthesize_implied_crossing_bores(
            layout, dict(nodes_m), [mapped_way], None,
            low_connector_max_gap_m=0.0, node_tags=node_tags)
        bores = _bore_pieces(ways)
        assert len(bores) == 1
        # Mapped-tunnel bore pieces still assert as excavated tunnels.
        assert bores[0][2].get("tunnel") == "yes"


# ──────────────────────────────────────────────────────────────────
# C. OSM cache node-tag round-trip
# ──────────────────────────────────────────────────────────────────
_ROUND_TRIP_OSM_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<osm version="0.6" generator="test">\n'
    '  <node id="1" lat="60.0000000" lon="-1.0000000">\n'
    '    <tag k="barrier" v="lift_gate"/>\n'
    '    <tag k="aeroway" v="aircraft_crossing"/>\n'
    '  </node>\n'
    '  <node id="2" lat="60.0010000" lon="-1.0010000"/>\n'
    '  <way id="10" version="1">\n'
    '    <nd ref="1"/>\n'
    '    <nd ref="2"/>\n'
    '    <tag k="highway" v="primary"/>\n'
    '    <tag k="width" v="6.5"/>\n'
    '    <tag k="lanes" v="2"/>\n'
    '  </way>\n'
    '</osm>'
)

_SCHEMA = "2026-07-16"


class TestOsmNodeTagCacheRoundTrip:
    """Whitelisted node tags and the schema marker survive a write-read
    cycle through ``OSM_layer`` and ``osm_load._load_osm_tile``."""

    def _build_layer(self) -> OSM.OSM_layer:
        """Parse the fixture XML through ``update_dicosm`` with the
        node/way tag whitelists the road-layer download uses."""
        layer = OSM.OSM_layer()
        input_tags = {"n": [], "w": [("highway", "primary")], "r": []}
        target_tags = {
            "n": [("barrier", ""), ("aeroway", "")],
            "w": [("highway", "primary"), ("width", ""), ("lanes", "")],
            "r": [],
        }
        ok = layer.update_dicosm(
            _ROUND_TRIP_OSM_XML.encode("utf-8"), input_tags, target_tags)
        assert ok == 1
        return layer

    def test_schema_marker_written_and_matched(self, tmp_path) -> None:
        """``write_to_file`` stamps the schema marker;
        ``_cached_osm_schema_matches`` reads it back correctly."""
        layer = self._build_layer()
        path = tmp_path / "cache_schema.osm"
        assert layer.write_to_file(
            str(path), header_attributes={"o4_tag_schema": _SCHEMA}) == 1

        assert OSM._cached_osm_schema_matches(str(path), _SCHEMA) is True
        assert OSM._cached_osm_schema_matches(str(path), "other") is False
        # An empty schema request accepts any cache (legacy behaviour).
        assert OSM._cached_osm_schema_matches(str(path), "") is True

    def test_node_and_way_tags_round_trip(self, tmp_path) -> None:
        """The node tags and the way's width/lanes survive the cache
        write and the ``_load_osm_tile`` read-back."""
        layer = self._build_layer()
        # _load_osm_tile is lru_cached by path — a unique file name keeps
        # this read independent of any other test's cache file.
        path = tmp_path / "cache_round_trip.osm"
        assert layer.write_to_file(
            str(path), header_attributes={"o4_tag_schema": _SCHEMA}) == 1

        nodes, ways, _relations, node_tags = osm_load._load_osm_tile(
            str(path))

        # The tagged node carries its whitelisted level-crossing evidence.
        tagged = [tags for tags in node_tags.values()
                  if tags.get("barrier") == "lift_gate"]
        assert len(tagged) == 1
        assert tagged[0].get("aeroway") == "aircraft_crossing"

        # The road way carries its own OSM measurements through the cache.
        primary_ways = [w for w in ways
                        if w[2].get("highway") == "primary"]
        assert len(primary_ways) == 1
        way_tags = primary_ways[0][2]
        assert way_tags.get("width") == "6.5"
        assert way_tags.get("lanes") == "2"
