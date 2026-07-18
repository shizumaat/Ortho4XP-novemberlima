"""Tests for the interior-edge-crossing detector added to
``tools/chain_divergence_audit.py`` (part-36 queue item 6).

The detector reports pairs of way edges from DIFFERENT ways whose
interiors transversally cross at a single point sitting strictly inside
both edges (farther than the 1 mm endpoint tolerance from every
endpoint).  Endpoint touches, shared-vertex joins and parallel slivers
must NOT be counted — those belong to the T-vertex, coincident-node and
near-parallel classes.

Synthetic fixture (longitude, latitude), built inline and written to a
temporary ``.osm`` so it exercises the exact ``_load`` / ``analyze``
path the lab uses on real patch output:

    node 1: (10.0000, 50.0000)      node 2: (10.0010, 50.0010)
    node 3: (10.0000, 50.0010)      node 4: (10.0010, 50.0000)
    node 5: (10.0020, 50.0000)

    way 100 taxiway: 1 -> 2         # one diagonal of the square
    way 200 runway : 3 -> 4         # the other diagonal -> a true X
    way 300 apron  : 2 -> 5         # shares node 2 (endpoint touch);
                                    # also runs parallel to way 200

The taxiway and runway diagonals cross at the centre (10.0005, 50.0005):
exactly ONE interior crossing.  The apron edge only touches the taxiway
at their shared endpoint and is parallel to the runway diagonal, so it
adds no crossing.
"""

from __future__ import annotations

import importlib.util
import os

TOOLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")


def _load_audit_module():
    spec = importlib.util.spec_from_file_location(
        "chain_divergence_audit",
        os.path.join(TOOLS_DIR, "chain_divergence_audit.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_NODES = [
    (1, 10.0000, 50.0000),
    (2, 10.0010, 50.0010),
    (3, 10.0000, 50.0010),
    (4, 10.0010, 50.0000),
    (5, 10.0020, 50.0000),
]
_WAYS = [
    (100, "taxiway", [1, 2]),
    (200, "runway", [3, 4]),
    (300, "apron", [2, 5]),
]


def _write_osm(path):
    lines = ["<?xml version='1.0' encoding='UTF-8'?>", "<osm version='0.6'>"]
    for nid, lon, lat in _NODES:
        lines.append(f"  <node id='{nid}' lat='{lat:.7f}' lon='{lon:.7f}'/>")
    for wid, role, refs in _WAYS:
        lines.append(f"  <way id='{wid}'>")
        for r in refs:
            lines.append(f"    <nd ref='{r}'/>")
        lines.append(f"    <tag k='o4_feature' v='{role}'/>")
        lines.append("  </way>")
    lines.append("</osm>")
    with open(path, "w") as handle:
        handle.write("\n".join(lines))


def test_interior_edge_crossing_detects_single_transversal(tmp_path, capsys):
    module = _load_audit_module()
    osm_path = os.path.join(str(tmp_path), "synthetic_crossing.osm")
    _write_osm(osm_path)

    total_tv, near_parallel, crossings, _self_crossings = module.analyze(
        osm_path)

    # exactly one interior crossing: the taxiway/runway diagonals.
    assert crossings == 1, f"expected 1 crossing, got {crossings}"
    # the shared-endpoint apron edge is NOT a crossing.
    out = capsys.readouterr().out
    assert "runway~taxiway" in out    # role pair is sorted alphabetically


def test_endpoint_touch_and_parallel_are_not_crossings(tmp_path):
    """Two ways sharing an endpoint, and two parallel ways, yield zero
    crossings on their own."""
    module = _load_audit_module()
    osm_path = os.path.join(str(tmp_path), "no_crossing.osm")
    # only the touch + parallel ways, no transversal diagonal pair.
    lines = ["<osm version='0.6'>"]
    for nid, lon, lat in [(2, 10.0010, 50.0010), (4, 10.0010, 50.0000),
                          (5, 10.0020, 50.0000), (3, 10.0000, 50.0010)]:
        lines.append(f"  <node id='{nid}' lat='{lat:.7f}' lon='{lon:.7f}'/>")
    # way 200 (runway 3->4) and way 300 (apron 2->5) are parallel
    # diagonals; they neither cross nor share a vertex.
    for wid, role, refs in [(200, "runway", [3, 4]), (300, "apron", [2, 5])]:
        lines.append(f"  <way id='{wid}'>")
        for r in refs:
            lines.append(f"    <nd ref='{r}'/>")
        lines.append(f"    <tag k='o4_feature' v='{role}'/>")
        lines.append("  </way>")
    lines.append("</osm>")
    with open(osm_path, "w") as handle:
        handle.write("\n".join(lines))

    _tv, _np, crossings, _self_crossings = module.analyze(osm_path)
    assert crossings == 0, f"parallel/touch pair miscounted: {crossings}"
