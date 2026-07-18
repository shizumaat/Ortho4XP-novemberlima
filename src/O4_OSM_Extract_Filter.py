"""Filter local OpenStreetMap extracts into Overpass-equivalent OSM XML.

This module is the filtering half of the OSM regional-extracts feature
(spec: ``docs/specs/osm-regional-extracts-spec.md`` section 5).  Given one
or more local extract files (Geofabrik ``.osm.pbf`` snapshots, or ``.osm`` /
``.osm.bz2`` XML), it reproduces the byte-level result of the Overpass query

    (statement1(bbox); statement2(bbox); ...); (._; >>;); out meta;

so the output can be handed to ``OSM_layer.update_dicosm`` at the single
``get_overpass_data`` call site with no other pipeline change.

Design
------
The Overpass semantics we mirror are:

* **Selection** — an element is selected when its type matches a statement
  prefix (``node`` / ``way`` / ``rel``), its tags match that statement (the
  key is present; the value is equal when the statement carries one; several
  statements are OR-ed), AND its geometry touches the bbox: a node by its own
  coordinates, a way by any of its nodes, a relation by any member node or by
  any member way that itself touches the bbox.
* **Downward closure** (Overpass ``>>``) — every selected relation drags in
  its member ways and member nodes, and every included way drags in all of
  its nodes, *even when those lie outside the bbox*.  Nested relation members
  are ignored, matching the downstream consumer which also ignores
  relation-in-relation members.

Passes and memory discipline
----------------------------
Each extract file is read three times with a light ``osmium.SimpleHandler``.
Only bbox-scoped and closure-scoped identifiers are held in Python; whole
country files are never materialised:

1. selection pass — records which node ids fall inside the bbox, which way
   ids touch it, the ids of directly selected ways/nodes, and the selected
   relations (their members and tags).  Because OSM files stream nodes, then
   ways, then relations, membership of earlier types is already known when a
   later type is examined.
2. way-gather pass — for every needed way (selected or pulled in by a
   relation) stores its node refs and tags, and grows the needed-node set.
3. node-gather pass — for every needed node stores its coordinates and tags.

Node coordinates are read straight off the node objects, so no on-disk node
location index is required.

Missing referenced nodes
------------------------
``OSM_layer.update_dicosm`` resolves each ``<nd ref=...>`` through a strict
dictionary lookup and raises if the node was never emitted.  A way whose
nodes are not all present in its extract file is therefore DROPPED rather
than emitted with dangling refs (Geofabrik's complete-ways extracts make
this vanishingly rare).  Relation members that point at a dropped or absent
way are left in place: the consumer tolerates missing way members.

Elements are deduplicated by ``(type, id)`` across the extract files, first
file wins.
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, List, Optional, Tuple

import osmium


class ExtractFilterError(Exception):
    """An extract file could not be read or filtered."""


# A tag matcher is ``(key, value_or_None)``: value None means key-existence.
_Matcher = Tuple[str, Optional[str]]
_Matchers = Dict[str, List[_Matcher]]

# Local per-file collections handed to the serializer.
_NodeData = Tuple[float, float, List[Tuple[str, str]]]  # (lat, lon, tags)
_WayData = Tuple[List[int], List[Tuple[str, str]]]  # (node refs, tags)
_RelData = Tuple[List[Tuple[str, int, str]], List[Tuple[str, str]]]  # (members, tags)


def _parse_statements(statements: Iterable[str]) -> _Matchers:
    """Turn Overpass statement strings into per-type tag matchers.

    Parses exactly the way ``O4_OSM_Utils`` does: ``statement.split('"')``
    yields ``items``; ``items[0]`` prefixes the element type, ``items[1]`` is
    the key, and ``items[3]`` (when present) is the required value.  A
    statement without a value part is a key-existence match.
    """
    matchers: _Matchers = {"node": [], "way": [], "relation": []}
    for statement in statements:
        items = statement.split('"')
        prefix = items[0]
        if prefix.startswith("node"):
            osmtype = "node"
        elif prefix.startswith("way"):
            osmtype = "way"
        elif prefix.startswith("rel"):
            osmtype = "relation"
        else:
            # Unrecognised prefix: skip rather than guess.
            continue
        key = items[1]
        value = items[3] if len(items) > 3 else None
        matchers[osmtype].append((key, value))
    return matchers


_BoundingBox = Tuple[float, float, float, float]  # (lat_min, lon_min, lat_max, lon_max)


def _normalize_bounding_boxes(bounding_box) -> List[_BoundingBox]:
    """Accept one ``(lat_min, lon_min, lat_max, lon_max)`` box or a list
    of them; always return a list.

    The multi-box form exists because the osmium passes read the WHOLE
    extract file regardless of box size — one pass selecting against N
    boxes costs the same as one pass against one box, while N separate
    calls cost N full reads.  Callers with several disjoint areas (the
    per-airport inset footprint queries) pass the list; a single bounding
    RECTANGLE over disjoint areas would be wrong, sweeping up everything
    between them.
    """
    boxes = list(bounding_box)
    if not boxes:
        return []
    # Multi-box form: the first element is itself a box (a sequence),
    # not a coordinate scalar.  Sequence detection (rather than scalar
    # type checks) keeps numpy scalar coordinates classified correctly.
    if isinstance(boxes[0], (tuple, list)):
        return [tuple(float(value) for value in box) for box in boxes]
    if len(boxes) != 4:
        raise ExtractFilterError(
            "bounding_box must be a 4-tuple or a list of 4-tuples"
        )
    return [tuple(float(value) for value in boxes)]


def _tags_match(tags: "osmium.osm.TagList", matchers: List[_Matcher]) -> bool:
    """True when any matcher's key is present (and its value equals, when
    the matcher carries one)."""
    for key, value in matchers:
        actual = tags.get(key)
        if actual is None:
            continue
        if value is None or actual == value:
            return True
    return False


class _SelectionHandler(osmium.SimpleHandler):
    """Pass 1 — decide selected elements and seed the closure sets."""

    def __init__(self, matchers: _Matchers, bounding_boxes: List[_BoundingBox]):
        super().__init__()
        self._matchers = matchers
        self._boxes = list(bounding_boxes)
        # Shared state consumed by later passes.
        self.nodes_in_bbox: set = set()
        self.ways_touching: set = set()
        self.needed_way_ids: set = set()
        self.needed_node_ids: set = set()
        self.selected_rels: Dict[int, _RelData] = {}

    def _in_bbox(self, lat: float, lon: float) -> bool:
        for (lat_min, lon_min, lat_max, lon_max) in self._boxes:
            if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
                return True
        return False

    def node(self, n: "osmium.osm.Node") -> None:
        loc = n.location
        if not self._in_bbox(loc.lat, loc.lon):
            return
        self.nodes_in_bbox.add(n.id)
        if _tags_match(n.tags, self._matchers["node"]):
            # A directly selected node is its own closure.
            self.needed_node_ids.add(n.id)

    def way(self, w: "osmium.osm.Way") -> None:
        refs = [nd.ref for nd in w.nodes]
        if not any(ref in self.nodes_in_bbox for ref in refs):
            return
        self.ways_touching.add(w.id)
        if _tags_match(w.tags, self._matchers["way"]):
            self.needed_way_ids.add(w.id)

    def relation(self, r: "osmium.osm.Relation") -> None:
        touches = False
        way_member_refs: List[int] = []
        node_member_refs: List[int] = []
        for m in r.members:
            if m.type == "n":
                node_member_refs.append(m.ref)
                if m.ref in self.nodes_in_bbox:
                    touches = True
            elif m.type == "w":
                way_member_refs.append(m.ref)
                if m.ref in self.ways_touching:
                    touches = True
        if not (touches and _tags_match(r.tags, self._matchers["relation"])):
            return
        members = [(m.type, m.ref, m.role) for m in r.members]
        tags = [(t.k, t.v) for t in r.tags]
        self.selected_rels[r.id] = (members, tags)
        # Downward closure of the relation: its member ways and member nodes.
        self.needed_way_ids.update(way_member_refs)
        self.needed_node_ids.update(node_member_refs)


class _WayGatherHandler(osmium.SimpleHandler):
    """Pass 2 — collect the geometry/tags of every needed way and grow the
    needed-node set with those ways' nodes."""

    def __init__(self, needed_way_ids: set, needed_node_ids: set):
        super().__init__()
        self._needed_way_ids = needed_way_ids
        self._needed_node_ids = needed_node_ids
        self.ways: Dict[int, _WayData] = {}

    def way(self, w: "osmium.osm.Way") -> None:
        if w.id not in self._needed_way_ids:
            return
        refs = [nd.ref for nd in w.nodes]
        tags = [(t.k, t.v) for t in w.tags]
        self.ways[w.id] = (refs, tags)
        self._needed_node_ids.update(refs)


class _NodeGatherHandler(osmium.SimpleHandler):
    """Pass 3 — collect the coordinates/tags of every needed node."""

    def __init__(self, needed_node_ids: set):
        super().__init__()
        self._needed_node_ids = needed_node_ids
        self.nodes: Dict[int, _NodeData] = {}

    def node(self, n: "osmium.osm.Node") -> None:
        if n.id not in self._needed_node_ids:
            return
        loc = n.location
        tags = [(t.k, t.v) for t in n.tags]
        self.nodes[n.id] = (loc.lat, loc.lon, tags)


def _process_extract(
    path: str,
    matchers: _Matchers,
    bounding_boxes: List[_BoundingBox],
) -> Tuple[Dict[int, _NodeData], Dict[int, _WayData], Dict[int, _RelData]]:
    """Run the three passes over one extract file and return its selected +
    closure nodes, ways and relations.  Ways whose nodes are not all present
    in the file are dropped."""
    if not os.path.isfile(path):
        raise ExtractFilterError("extract file not found: " + str(path))
    try:
        selection = _SelectionHandler(matchers, bounding_boxes)
        selection.apply_file(path)

        way_gather = _WayGatherHandler(
            selection.needed_way_ids, selection.needed_node_ids
        )
        way_gather.apply_file(path)

        node_gather = _NodeGatherHandler(selection.needed_node_ids)
        node_gather.apply_file(path)
    except ExtractFilterError:
        raise
    except Exception as e:  # osmium raises RuntimeError on unreadable/corrupt input
        raise ExtractFilterError(
            "could not read extract " + str(path) + ": " + str(e)
        ) from e

    nodes = node_gather.nodes
    ways = {
        wid: data
        for wid, data in way_gather.ways.items()
        if all(ref in nodes for ref in data[0])
    }
    return nodes, ways, selection.selected_rels


def _xml_escape(text: str) -> str:
    """Escape a string for an XML attribute value.  Newlines/tabs are turned
    into numeric entities as well, so that the line-oriented consumer parser
    never sees a tag value split across lines."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\n", "&#10;")
        .replace("\r", "&#13;")
        .replace("\t", "&#9;")
    )


def _serialize(
    nodes: Dict[int, _NodeData],
    ways: Dict[int, _WayData],
    rels: Dict[int, _RelData],
) -> bytes:
    """Emit nodes, then ways, then relations as Overpass-style OSM XML.

    Every element carries ``version="1"`` (harmless, mirrors ``out meta``).
    Each element and each child sits on its own line so the consumer's
    line-by-line, quote-splitting parser reads them exactly as it reads an
    Overpass response.
    """
    out: List[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<osm version="0.6" generator="Ortho4XP-regional-extract-filter">\n',
    ]

    def emit_tags(tags: List[Tuple[str, str]]) -> None:
        for key, value in tags:
            out.append(
                '    <tag k="'
                + _xml_escape(key)
                + '" v="'
                + _xml_escape(value)
                + '"/>\n'
            )

    for node_id in sorted(nodes):
        lat, lon, tags = nodes[node_id]
        head = (
            '  <node id="'
            + str(node_id)
            + '" lat="'
            + "{:.7f}".format(lat)
            + '" lon="'
            + "{:.7f}".format(lon)
            + '" version="1"'
        )
        if tags:
            out.append(head + ">\n")
            emit_tags(tags)
            out.append("  </node>\n")
        else:
            out.append(head + "/>\n")

    for way_id in sorted(ways):
        refs, tags = ways[way_id]
        out.append('  <way id="' + str(way_id) + '" version="1">\n')
        for ref in refs:
            out.append('    <nd ref="' + str(ref) + '"/>\n')
        emit_tags(tags)
        out.append("  </way>\n")

    for rel_id in sorted(rels):
        members, tags = rels[rel_id]
        out.append('  <relation id="' + str(rel_id) + '" version="1">\n')
        for mtype, ref, role in members:
            # osmium member types are single chars; the consumer expects the
            # long form ("way"/"node"/"relation").
            long_type = {"n": "node", "w": "way", "r": "relation"}.get(mtype, mtype)
            out.append(
                '    <member type="'
                + long_type
                + '" ref="'
                + str(ref)
                + '" role="'
                + _xml_escape(role)
                + '"/>\n'
            )
        emit_tags(tags)
        out.append("  </relation>\n")

    out.append("</osm>")
    return "".join(out).encode("utf-8")


def filter_extracts_to_osm_xml(
    extract_paths: Iterable[str],
    statements: Iterable[str],
    bounding_box: Tuple[float, float, float, float],
) -> bytes:
    """OSM XML bytes reproducing Overpass union + deep-recursion semantics.

    extract_paths: iterable of local extract file paths (.osm.pbf, .osm,
        .osm.bz2).
    statements: iterable of Overpass statement strings exactly as used in
        O4_Vector_Map, e.g. ``way["natural"="water"]``,
        ``rel["waterway"="riverbank"]``, ``node["aeroway"]`` (value part
        optional -> key-existence match).
    bounding_box: ``(lat_min, lon_min, lat_max, lon_max)`` in degrees, or
        a LIST of such boxes — one filtering pass then selects elements
        inside ANY of the boxes (see :func:`_normalize_bounding_boxes` for
        why that beats one call per box).

    Returns utf-8 XML bytes: an ``<osm version="0.6" generator="...">``
    document containing nodes first, then ways, then relations, with the full
    downward closure of every selected element.  Elements are deduplicated by
    ``(type, id)`` across the extract files, first file wins.  Ways whose
    nodes are not all present in their extract are dropped (see module
    docstring).  Raises ExtractFilterError when an extract file is
    missing/unreadable.
    """
    matchers = _parse_statements(statements)
    bounding_boxes = _normalize_bounding_boxes(bounding_box)

    merged_nodes: Dict[int, _NodeData] = {}
    merged_ways: Dict[int, _WayData] = {}
    merged_rels: Dict[int, _RelData] = {}

    for path in extract_paths:
        nodes, ways, rels = _process_extract(path, matchers, bounding_boxes)
        # First file wins: setdefault only fills ids not already present.
        for node_id, data in nodes.items():
            merged_nodes.setdefault(node_id, data)
        for way_id, data in ways.items():
            merged_ways.setdefault(way_id, data)
        for rel_id, data in rels.items():
            merged_rels.setdefault(rel_id, data)

    return _serialize(merged_nodes, merged_ways, merged_rels)
