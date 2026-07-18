"""Regression tests: per-vertex ``node_altitudes`` must survive every
``to_osm`` mutation (EGGW tunnel-plate loss, measured 2026-07-17).

Mechanism under test (root-caused 2026-07-18): the nid-level final weld
inserts on-edge node references from partner ways into a value-carrying
ring.  When such a node was interned WITHOUT an altitude claim (its
first-writer way carried no altitude model, or its ``node_altitudes``
was misaligned and silently dropped), the single consensus-less node
knocked the ENTIRE way off the per-node ``alt_abs`` emission path
(``have_all``), and the fallback branch had no ``node_altitudes``
handling at all — so a 4-corner roof quad shipped as a 6-7-node way
with ``alt_abs`` on only the vertices other ways happened to claim,
and the mesh dropped the rest onto raw DEM.

The fixes asserted here:
* unclaimed welded-in nodes are backfilled with the host ring's
  interpolated altitude, so the way keeps full per-node emission;
* the closing-repeat trim of a ``node_altitudes`` list is keyed on
  LENGTH, not on ``elevs[0] == elevs[-1]`` (which mis-trimmed an OPEN
  ``[H, L, L, H]`` quad list and dropped all four values);
* a genuinely misaligned ``node_altitudes`` list degrades LOUDLY to a
  way-level tag instead of shipping an unconstrained way.
"""
import re
import tempfile
from pathlib import Path

from shapely.geometry import Polygon

from auto_patch.layout import (
    BuiltShape,
    PavementLayout,
    ROLE_RETAINING_WALL,
    ROLE_TUNNEL_RAMP,
)


def _emit_and_parse(layout):
    """Call to_osm then parse the resulting XML.

    Returns (nodes, ways, node_alts) — same shape as the helper in
    ``test_layout.py``.
    """
    with tempfile.NamedTemporaryFile(
            mode="r", suffix=".osm", delete=False) as f:
        path = f.name
    try:
        layout.to_osm(path)
        text = Path(path).read_text()
    finally:
        Path(path).unlink()

    node_re = re.compile(
        r"""<node id='(-?\d+)'[^>]*lat='([^']+)' lon='([^']+)'""")
    node_alt_re = re.compile(
        r"""<node id='(-?\d+)'[^>]*?>\s*<tag k='alt_abs' v='([^']+)'""",
        re.DOTALL)
    way_open_re = re.compile(r"""<way id='(-?\d+)'""")
    nd_re = re.compile(r"""<nd ref='(-?\d+)'""")
    tag_re = re.compile(r"""<tag k='([^']+)' v='([^']+)'""")

    nodes = {}
    for m in node_re.finditer(text):
        nodes[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
    node_alts = {}
    for m in node_alt_re.finditer(text):
        node_alts[int(m.group(1))] = float(m.group(2))
    ways = []
    way_blocks = re.findall(
        r"<way id='-?\d+'[^>]*>(.*?)</way>", text, flags=re.DOTALL)
    for i, m_open in enumerate(way_open_re.finditer(text)):
        wid = int(m_open.group(1))
        body = way_blocks[i]
        nds = [int(x) for x in nd_re.findall(body)]
        tags = {k: v for k, v in tag_re.findall(body)}
        ways.append((wid, nds, tags))
    return nodes, ways, node_alts


EH, EL = 130.0, 118.0      # roof quad high / low end altitudes


def _layout_with_roof(roof_kwargs, wall_kwargs):
    """A POST-SOLVE tunnel-plate scene in miniature: a 60 m sloped roof
    quad (high end x=0, low end x=60) plus a wall polygon along the
    quad's south edge whose corners at x=20 and x=40 lie exactly ON
    that edge — T-vertices the nid-level final weld inserts into the
    roof ring."""
    layout = PavementLayout(icao="KFAKE", anchor=(51.87, -0.37))
    roof = Polygon([(0, 0), (60, 0), (60, 10), (0, 10)])
    layout.shapes.append(BuiltShape(
        polygon=roof, role=ROLE_TUNNEL_RAMP, ref="tunnel_roof",
        **roof_kwargs))
    wall = Polygon([(20, 0), (40, 0), (40, -3), (20, -3)])
    layout.shapes.append(BuiltShape(
        polygon=wall, role=ROLE_RETAINING_WALL, ref="tunnel_wall",
        **wall_kwargs))
    return layout


def _roof_way(ways):
    for wid, nds, tags in ways:
        if tags.get("ref") == "tunnel_roof":
            return nds, tags
    raise AssertionError("tunnel_roof way not emitted")


def _assert_full_per_node(nds, tags, node_alts):
    """Every open-ring vertex of the way carries ``alt_abs``, with the
    quad's corner values at the ends and the edge lerp in between."""
    open_nds = nds[:-1]
    missing = [n for n in open_nds if n not in node_alts]
    assert not missing, (
        f"{len(missing)}/{len(open_nds)} vertices shipped without "
        f"alt_abs — those drop onto raw DEM (the EGGW collapse)")
    vals = [node_alts[n] for n in open_nds]
    assert max(vals) == EH and min(vals) == EL
    # No way-level altitude tag needed once fully per-node.
    assert "altitude" not in tags


def test_weld_inserted_unclaimed_node_keeps_per_vertex_emission():
    """A welded-in node from a way with NO altitude model used to knock
    the roof quad off per-node emission entirely (0 of 6 vertices with
    alt_abs).  The backfill now gives it the host edge's lerp."""
    layout = _layout_with_roof(
        dict(node_altitudes=[EH, EL, EL, EH, EH]), {})
    _nodes, ways, node_alts = _emit_and_parse(layout)
    nds, tags = _roof_way(ways)
    assert len(nds) - 1 == 6            # 4 corners + 2 welded T-vertices
    _assert_full_per_node(nds, tags, node_alts)
    # The welded-in nodes carry the roof edge's interpolated values
    # (130 -> 118 over 60 m: 126 at x=20, 122 at x=40).
    vals = sorted(node_alts[n] for n in nds[:-1])
    assert vals == [118.0, 118.0, 122.0, 126.0, 130.0, 130.0]


def test_weld_partner_with_misaligned_node_altitudes():
    """A partner whose ``node_altitudes`` is misaligned with its ring is
    dropped LOUDLY and interns unvalued — the same unclaimed-node class.
    The roof quad must still ship fully per-node."""
    layout = _layout_with_roof(
        dict(node_altitudes=[EH, EL, EL, EH, EH]),
        dict(node_altitudes=[126.0, 122.0, 122.0]))     # 3 for 4 verts
    _nodes, ways, node_alts = _emit_and_parse(layout)
    nds, tags = _roof_way(ways)
    _assert_full_per_node(nds, tags, node_alts)


def test_high_low_quad_with_unclaimed_weld_partner():
    """The retired-input hi/lo form used to degrade to a FLAT mean
    ``altitude`` way tag in the same scene (slope lost).  It now ships
    fully per-node like the node_altitudes form."""
    layout = _layout_with_roof(
        dict(altitude_high=EH, altitude_low=EL), {})
    _nodes, ways, node_alts = _emit_and_parse(layout)
    nds, tags = _roof_way(ways)
    _assert_full_per_node(nds, tags, node_alts)


def test_open_hllh_node_altitudes_not_mistrimmed():
    """An OPEN 4-element ``[H, L, L, H]`` list has equal first/last
    values, which the old value-keyed closing-repeat trim cut to 3
    entries — misaligning the list and dropping all four values.  The
    trim is now keyed on length."""
    layout = PavementLayout(icao="KFAKE", anchor=(51.87, -0.37))
    roof = Polygon([(0, 0), (60, 0), (60, 10), (0, 10)])
    layout.shapes.append(BuiltShape(
        polygon=roof, role=ROLE_TUNNEL_RAMP, ref="tunnel_roof",
        node_altitudes=[EH, EL, EL, EH]))               # open form
    _nodes, ways, node_alts = _emit_and_parse(layout)
    nds, tags = _roof_way(ways)
    _assert_full_per_node(nds, tags, node_alts)
    vals = sorted(node_alts[n] for n in nds[:-1])
    assert vals == [118.0, 118.0, 130.0, 130.0]


def test_lone_misaligned_shape_degrades_to_way_tag_not_unconstrained():
    """A value-carrying shape whose ring lost ALL claims (misaligned
    list, no partners to backfill from) must still ship SOME altitude
    constraint — the flat-mean way tag — never an unconstrained way."""
    layout = PavementLayout(icao="KFAKE", anchor=(51.87, -0.37))
    roof = Polygon([(0, 0), (60, 0), (60, 10), (0, 10)])
    layout.shapes.append(BuiltShape(
        polygon=roof, role=ROLE_TUNNEL_RAMP, ref="tunnel_roof",
        node_altitudes=[EH, EL, EL]))                   # 3 for 4 verts
    _nodes, ways, _node_alts = _emit_and_parse(layout)
    _nds, tags = _roof_way(ways)
    assert "altitude" in tags, (
        "misaligned per-vertex shape shipped with no altitude "
        "constraint at all")


def test_control_roof_quad_alone_ships_per_node():
    """Control: an unmolested node_altitudes quad ships all 4 corners
    as per-node ``alt_abs``."""
    layout = PavementLayout(icao="KFAKE", anchor=(51.87, -0.37))
    roof = Polygon([(0, 0), (60, 0), (60, 10), (0, 10)])
    layout.shapes.append(BuiltShape(
        polygon=roof, role=ROLE_TUNNEL_RAMP, ref="tunnel_roof",
        node_altitudes=[EH, EL, EL, EH, EH]))
    _nodes, ways, node_alts = _emit_and_parse(layout)
    nds, tags = _roof_way(ways)
    assert len(nds) - 1 == 4
    _assert_full_per_node(nds, tags, node_alts)
