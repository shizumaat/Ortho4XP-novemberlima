"""Filter local OSM extracts into Overpass-equivalent XML
(docs/specs/osm-regional-extracts-spec.md section 5).

Headless: hand-written ``.osm`` XML fixtures under ``tmp_path`` (pyosmium
reads XML natively), no network, no pbf, no X-Plane install.  The final
round-trip test proves the emitted bytes feed ``OSM_layer.update_dicosm``
exactly like an Overpass response.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import O4_OSM_Extract_Filter as FILTER  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers: build tiny .osm XML documents on disk.
# ---------------------------------------------------------------------------


def _osm_doc(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<osm version="0.6" generator="test-fixture">\n'
        + body
        + "</osm>\n"
    )


def _node(node_id, lat, lon, tags=None):
    tags = tags or {}
    head = '  <node id="%d" lat="%s" lon="%s" version="1"' % (node_id, lat, lon)
    if not tags:
        return head + "/>\n"
    out = head + ">\n"
    for k, v in tags.items():
        out += '    <tag k="%s" v="%s"/>\n' % (k, v)
    out += "  </node>\n"
    return out


def _way(way_id, refs, tags=None):
    tags = tags or {}
    out = '  <way id="%d" version="1">\n' % way_id
    for ref in refs:
        out += '    <nd ref="%d"/>\n' % ref
    for k, v in tags.items():
        out += '    <tag k="%s" v="%s"/>\n' % (k, v)
    out += "  </way>\n"
    return out


def _relation(rel_id, members, tags=None):
    """members: list of (type, ref, role) with type in {'way','node'}."""
    tags = tags or {}
    out = '  <relation id="%d" version="1">\n' % rel_id
    for mtype, ref, role in members:
        out += '    <member type="%s" ref="%d" role="%s"/>\n' % (mtype, ref, role)
    for k, v in tags.items():
        out += '    <tag k="%s" v="%s"/>\n' % (k, v)
    out += "  </relation>\n"
    return out


def _write(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(_osm_doc(body))
    return str(path)


# The bbox used by most tests: a 1x1 square around (0,0)..(1,1).
BBOX = (0.0, 0.0, 1.0, 1.0)


def _parse_ids(xml_bytes):
    """Cheap structural read of the emitted XML: returns (node_ids, way_ids,
    rel_ids) as ordered lists of the raw fixture ids, using the same
    line/quote conventions as the consumer."""
    nodes, ways, rels = [], [], []
    for line in xml_bytes.decode("utf-8").splitlines():
        items = line.split('"')
        if "<node id=" in items[0]:
            nodes.append(int(items[1]))
        elif "<way id=" in items[0]:
            ways.append(int(items[1]))
        elif "<relation id=" in items[0]:
            rels.append(int(items[1]))
    return nodes, ways, rels


# ---------------------------------------------------------------------------
# 1. tag + bbox match returns the way with all nodes; a same-tag way wholly
#    outside the bbox is not returned.
# ---------------------------------------------------------------------------


def test_matched_way_returned_with_nodes_outside_way_excluded(tmp_path):
    body = (
        _node(1, "0.5", "0.5")
        + _node(2, "0.6", "0.6")
        + _node(3, "5.0", "5.0")
        + _node(4, "5.1", "5.1")
        + _way(10, [1, 2], {"natural": "water"})
        + _way(20, [3, 4], {"natural": "water"})
    )
    path = _write(tmp_path, "a.osm", body)

    xml = FILTER.filter_extracts_to_osm_xml(
        [path], ['way["natural"="water"]'], BBOX
    )
    nodes, ways, rels = _parse_ids(xml)

    assert 10 in ways
    assert 20 not in ways  # wholly outside the bbox
    assert {1, 2} <= set(nodes)  # way 10's nodes came along
    assert 3 not in nodes and 4 not in nodes


# ---------------------------------------------------------------------------
# 2. a way with a DIFFERENT tag inside the bbox is not returned.
# ---------------------------------------------------------------------------


def test_wrong_tag_way_inside_bbox_excluded(tmp_path):
    body = (
        _node(1, "0.5", "0.5")
        + _node(2, "0.6", "0.6")
        + _way(10, [1, 2], {"highway": "residential"})
    )
    path = _write(tmp_path, "a.osm", body)

    xml = FILTER.filter_extracts_to_osm_xml(
        [path], ['way["natural"="water"]'], BBOX
    )
    _, ways, _ = _parse_ids(xml)
    assert ways == []


# ---------------------------------------------------------------------------
# 3. key-only statements match any value.
# ---------------------------------------------------------------------------


def test_key_only_statement_matches_any_value(tmp_path):
    body = (
        _node(1, "0.5", "0.5", {"aeroway": "gate"})
        + _node(2, "0.6", "0.6", {"aeroway": "helipad"})
        + _node(3, "0.7", "0.7", {"amenity": "bench"})
    )
    path = _write(tmp_path, "a.osm", body)

    xml = FILTER.filter_extracts_to_osm_xml([path], ['node["aeroway"]'], BBOX)
    nodes, _, _ = _parse_ids(xml)
    assert 1 in nodes and 2 in nodes
    assert 3 not in nodes  # no aeroway tag


# ---------------------------------------------------------------------------
# 4. a selected relation pulls in member ways/nodes lying OUTSIDE the bbox,
#    and the member ways' nodes come along (downward closure).
# ---------------------------------------------------------------------------


def test_relation_closure_pulls_outside_members(tmp_path):
    body = (
        # A node inside the bbox that anchors the relation.
        _node(1, "0.5", "0.5")
        # An outer way entirely OUTSIDE the bbox.
        + _node(2, "9.0", "9.0")
        + _node(3, "9.1", "9.1")
        + _way(50, [2, 3])
        # A member way that touches the bbox (node 1) so the relation is
        # selected even though its tag would otherwise need geometry proof.
        + _node(4, "0.4", "0.4")
        + _way(51, [1, 4])
        + _relation(
            100,
            [("way", 50, "outer"), ("way", 51, "outer")],
            {"waterway": "riverbank"},
        )
    )
    path = _write(tmp_path, "a.osm", body)

    xml = FILTER.filter_extracts_to_osm_xml(
        [path], ['rel["waterway"="riverbank"]'], BBOX
    )
    nodes, ways, rels = _parse_ids(xml)

    assert 100 in rels
    # Both member ways present, including the one fully outside the bbox.
    assert 50 in ways and 51 in ways
    # And the outside way's nodes came with it.
    assert 2 in nodes and 3 in nodes
    assert 1 in nodes and 4 in nodes


# ---------------------------------------------------------------------------
# 5. dedup across two extract files carrying the same element id.
# ---------------------------------------------------------------------------


def test_dedup_across_files_first_wins(tmp_path):
    body_a = (
        _node(1, "0.5", "0.5")
        + _node(2, "0.6", "0.6")
        + _way(10, [1, 2], {"natural": "water"})
    )
    body_b = (
        # Same ids, different coordinates: first file must win.
        _node(1, "0.9", "0.9")
        + _node(2, "0.8", "0.8")
        + _way(10, [1, 2], {"natural": "water"})
    )
    path_a = _write(tmp_path, "a.osm", body_a)
    path_b = _write(tmp_path, "b.osm", body_b)

    xml = FILTER.filter_extracts_to_osm_xml(
        [path_a, path_b], ['way["natural"="water"]'], BBOX
    )
    nodes, ways, _ = _parse_ids(xml)

    assert ways.count(10) == 1
    assert nodes.count(1) == 1 and nodes.count(2) == 1
    # First file's coordinates survived.
    assert 'lat="0.5000000"' in xml.decode("utf-8")
    assert 'lat="0.9000000"' not in xml.decode("utf-8")


# ---------------------------------------------------------------------------
# 6. round-trip through OSM_layer.update_dicosm.
# ---------------------------------------------------------------------------


def test_roundtrip_through_update_dicosm(tmp_path):
    import O4_OSM_Utils as OSM

    body = (
        _node(1, "0.5", "0.25")
        + _node(2, "0.6", "0.35")
        + _way(10, [1, 2], {"natural": "water"})
    )
    path = _write(tmp_path, "a.osm", body)

    xml = FILTER.filter_extracts_to_osm_xml(
        [path], ['way["natural"="water"]'], BBOX
    )

    layer = OSM.OSM_layer()
    # input_tags/target_tags None -> every way becomes a "first" catch, all
    # tags kept, mirroring a direct Overpass download.
    ret = layer.update_dicosm(xml, None, None)
    assert ret == 1

    assert len(layer.dicosmfirst["w"]) == 1
    wayid = next(iter(layer.dicosmfirst["w"]))
    assert layer.dicosmtags["w"][wayid]["natural"] == "water"
    assert len(layer.dicosmw[wayid]) == 2

    # Node coordinates are stored as (lon, lat); confirm they match the
    # fixture regardless of the parser's internal id remapping.
    coords = set(layer.dicosmn.values())
    assert (0.25, 0.5) in coords
    assert (0.35, 0.6) in coords


# ---------------------------------------------------------------------------
# 7. missing extract file raises ExtractFilterError.
# ---------------------------------------------------------------------------


def test_missing_extract_raises(tmp_path):
    missing = str(tmp_path / "does_not_exist.osm")
    with pytest.raises(FILTER.ExtractFilterError):
        FILTER.filter_extracts_to_osm_xml(
            [missing], ['way["natural"="water"]'], BBOX
        )


# ---------------------------------------------------------------------------
# 8. multi-box: one filtering pass selects elements inside ANY of the given
#    boxes and nothing between them (a bounding rectangle would leak those).
# ---------------------------------------------------------------------------


def test_multi_box_selects_all_boxes_and_nothing_between(tmp_path):
    body = (
        _node(1, "0.2", "0.2")
        + _node(2, "0.3", "0.3")  # in box A
        + _node(3, "2.2", "2.2")
        + _node(4, "2.3", "2.3")  # in box B
        + _node(5, "1.5", "1.5")
        + _node(6, "1.6", "1.6")  # between the boxes
        + _way(10, [1, 2], {"building": "yes"})
        + _way(20, [3, 4], {"building": "yes"})
        + _way(30, [5, 6], {"building": "yes"})
    )
    path = _write(tmp_path, "a.osm", body)

    boxes = [(0.0, 0.0, 1.0, 1.0), (2.0, 2.0, 3.0, 3.0)]
    xml = FILTER.filter_extracts_to_osm_xml([path], ['way["building"]'], boxes)
    nodes, ways, rels = _parse_ids(xml)

    assert 10 in ways and 20 in ways
    # Inside the boxes' bounding RECTANGLE but outside every box: excluded.
    assert 30 not in ways
    assert {1, 2, 3, 4} <= set(nodes)
    assert 5 not in nodes and 6 not in nodes


def test_single_box_tuple_and_one_element_list_are_equivalent(tmp_path):
    body = (
        _node(1, "0.5", "0.5")
        + _node(2, "0.6", "0.6")
        + _way(10, [1, 2], {"building": "yes"})
    )
    path = _write(tmp_path, "a.osm", body)

    as_tuple = FILTER.filter_extracts_to_osm_xml(
        [path], ['way["building"]'], BBOX
    )
    as_list = FILTER.filter_extracts_to_osm_xml(
        [path], ['way["building"]'], [BBOX]
    )
    assert as_tuple == as_list
