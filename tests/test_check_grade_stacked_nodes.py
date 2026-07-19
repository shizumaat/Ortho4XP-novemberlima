"""Unit tests: the structural stacked-node detector.

``tools/check_grade.py::_check_stacked_nodes`` enforces the owner ruling
of 2026-07-19: nodes can NEVER be stacked — two distinct OSM node ids at
the same coordinate are illegal regardless of their elevations ("if they
are in the same spot they must be merged and share the same elevation").
A genuine level change must be horizontally-offset wall geometry, never
coincident nodes rendering a bare near-vertical mesh tear.

Properties, one test each:

* two distinct node ids at one coordinate with different elevations are
  flagged, with the elevation spread reported;
* two distinct node ids at one coordinate with EQUAL elevations are NOT
  flagged — a same-value coordinate twin is the legal figure-8
  OSM-encoding artifact (an OSM ring cannot reference one node id
  twice); the mesh welds by coordinates into one vertex, so no tear
  exists and the ruling's "share the same elevation" holds;
* one node id shared by two ways is NOT flagged (that is the merge the
  invariant demands);
* distinct node ids beyond ``STACKED_NODE_XY_TOL_M`` are NOT flagged
  (near-adjacent pairs belong to the proximity / seam checks);
* a stacked pair without elevations is NOT flagged (both drape onto
  the DEM — one surface);
* a stacked pair referenced by several ways yields ONE violation row.

The synthetic OSM node ``lat``/``lon`` are treated directly as local
meters via an identity projection (same harness as the strip-seam
tests).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
for _p in (os.path.join(_ROOT, "tools"), os.path.join(_ROOT, "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check_grade  # noqa: E402


def _identity_meters(lat: float, lon: float) -> Tuple[float, float]:
    """Unit-test frame: a node's stored ``lat``/``lon`` ARE local meters
    ``(y, x)``, so planar gaps between synthetic nodes are exact."""
    return (lon, lat)


# A synthetic node: id, x-meter, y-meter, absolute altitude (None = no
# elevation tag on the node).
Node = Tuple[str, float, float, float]


def _write_osm(tmp_path: Path,
               ways_spec: List[Dict],
               filename: str = "stacked.osm") -> Path:
    """Emit a minimal X-Plane patch OSM file to ``tmp_path``.

    Same conventions as the strip-seam test harness; an altitude of
    ``None`` emits the node WITHOUT an ``alt_abs`` tag."""
    node_lines: List[str] = []
    emitted_nids: Dict[str, Node] = {}
    for spec in ways_spec:
        for (nid, x_m, y_m, alt) in spec["nodes"]:
            if nid in emitted_nids:
                continue
            emitted_nids[nid] = (nid, x_m, y_m, alt)
    for (nid, x_m, y_m, alt) in emitted_nids.values():
        if alt is None:
            node_lines.append(
                f"  <node id='{nid}' action='modify' visible='true' "
                f"lat='{y_m}' lon='{x_m}' />")
        else:
            node_lines.append(
                f"  <node id='{nid}' action='modify' visible='true' "
                f"lat='{y_m}' lon='{x_m}'>\n"
                f"    <tag k='alt_abs' v='{alt}' />\n"
                f"  </node>")
    way_lines: List[str] = []
    for spec in ways_spec:
        nds = "".join(
            f"    <nd ref='{nid}' />\n" for (nid, _, _, _) in spec["nodes"])
        way_lines.append(
            f"  <way id='{spec['wid']}' action='modify' visible='true'>\n"
            f"{nds}"
            f"    <tag k='role' v='{spec['role']}' />\n"
            f"    <tag k='shapeID' v='{spec['shapeID']}' />\n"
            f"  </way>")
    text = (
        "<?xml version='1.0' encoding='UTF-8'?>\n<osm version='0.6'>\n"
        + "\n".join(node_lines) + "\n"
        + "\n".join(way_lines) + "\n</osm>\n")
    out = tmp_path / filename
    out.write_text(text)
    return out


def _run(tmp_path: Path, ways_spec: List[Dict]):
    """Parse a synthetic OSM file and run the stacked-node check in the
    identity meter frame; returns the list of violations."""
    path = _write_osm(tmp_path, ways_spec)
    nodes, ways = check_grade._parse_osm(path)
    vertices, _edges = check_grade._build_vertex_edge_tables(
        nodes, ways, _identity_meters)
    return check_grade._check_stacked_nodes(vertices, ways)


# Far-flung padding nodes keep each way a legal >=3-node ring without
# minting extra coincident pairs.
def _pad_a() -> List[Node]:
    return [("-90", 0.0, 200.0, 10.0), ("-91", 0.0, 400.0, 10.0)]


def _pad_b() -> List[Node]:
    return [("-92", 200.0, 0.0, 10.0), ("-93", 400.0, 0.0, 10.0)]


def test_stacked_pair_with_different_elevations_is_flagged(tmp_path):
    ways_spec = [
        {"wid": "-10", "role": "graded_strip", "shapeID": "1",
         "nodes": [("-1", 0.0, 0.0, 10.0)] + _pad_a()},
        {"wid": "-20", "role": "apron", "shapeID": "2",
         "nodes": [("-2", 0.0, 0.0, 13.5)] + _pad_b()},
    ]
    violations = _run(tmp_path, ways_spec)
    assert len(violations) == 1, [
        (v.de_m, v.distance_m) for v in violations]
    v = violations[0]
    assert abs(v.de_m - 3.5) < 1e-6
    assert v.distance_m < check_grade.STACKED_NODE_XY_TOL_M


def test_stacked_pair_with_equal_elevations_is_not_flagged(tmp_path):
    # Same spot, same value, two node ids: the figure-8 OSM-encoding
    # twin (a ring cannot reference one nid twice).  The mesh welds by
    # coordinates into ONE vertex with one elevation — the ruling's
    # "share the same elevation" holds, no violation.
    ways_spec = [
        {"wid": "-10", "role": "apron", "shapeID": "1",
         "nodes": [("-1", 0.0, 0.0, 10.0)] + _pad_a()},
        {"wid": "-20", "role": "apron", "shapeID": "2",
         "nodes": [("-2", 0.0, 0.0, 10.0)] + _pad_b()},
    ]
    assert _run(tmp_path, ways_spec) == []


def test_shared_node_id_is_not_flagged(tmp_path):
    # One interned node referenced by both ways IS the merge the ruling
    # demands — nothing to flag.
    ways_spec = [
        {"wid": "-10", "role": "apron", "shapeID": "1",
         "nodes": [("-1", 0.0, 0.0, 10.0)] + _pad_a()},
        {"wid": "-20", "role": "graded_strip", "shapeID": "2",
         "nodes": [("-1", 0.0, 0.0, 10.0)] + _pad_b()},
    ]
    assert _run(tmp_path, ways_spec) == []


def test_nodes_beyond_xy_tolerance_are_not_flagged(tmp_path):
    # 0.3 m apart: distinct points (the emitter's canonical registry
    # spaces distinct points, and near-adjacent disagreement belongs to
    # the proximity / seam checks).
    ways_spec = [
        {"wid": "-10", "role": "apron", "shapeID": "1",
         "nodes": [("-1", 0.0, 0.0, 10.0)] + _pad_a()},
        {"wid": "-20", "role": "apron", "shapeID": "2",
         "nodes": [("-2", 0.3, 0.0, 13.5)] + _pad_b()},
    ]
    assert _run(tmp_path, ways_spec) == []


def test_stacked_pair_without_elevations_is_not_flagged(tmp_path):
    # Neither node carries an elevation tag: both drape onto the DEM —
    # one rendered surface, no contradictory constraint, no violation.
    ways_spec = [
        {"wid": "-10", "role": "boundary", "shapeID": "1",
         "nodes": [("-1", 0.0, 0.0, None)] + _pad_a()},
        {"wid": "-20", "role": "boundary", "shapeID": "2",
         "nodes": [("-2", 0.0, 0.0, None)] + _pad_b()},
    ]
    assert _run(tmp_path, ways_spec) == []


def test_stacked_pair_reported_once_across_many_ways(tmp_path):
    # Four ways reference the two stacked nodes (two ways each): the
    # violation is per NODE PAIR, not per way pair — one row.
    ways_spec = [
        {"wid": "-10", "role": "apron", "shapeID": "1",
         "nodes": [("-1", 0.0, 0.0, 10.0)] + _pad_a()},
        {"wid": "-11", "role": "junction", "shapeID": "3",
         "nodes": [("-1", 0.0, 0.0, 10.0)] + _pad_a()},
        {"wid": "-20", "role": "graded_strip", "shapeID": "2",
         "nodes": [("-2", 0.0, 0.0, 13.5)] + _pad_b()},
        {"wid": "-21", "role": "graded_strip", "shapeID": "4",
         "nodes": [("-2", 0.0, 0.0, 13.5)] + _pad_b()},
    ]
    violations = _run(tmp_path, ways_spec)
    assert len(violations) == 1
    assert abs(violations[0].de_m - 3.5) < 1e-6
