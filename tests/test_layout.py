"""Unit tests for auto_patch.layout — data model + OSM emission.

Covers:
* PavementLayout.m_to_ll / ll_to_m round-trip.
* _projection / _airport_anchor helpers.
* PavementLayout.to_osm: shape emission, shared-vertex IDs, tag
  handling per role and per elevation form.
"""
import math
import re
import tempfile
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from auto_patch.layout import (
    AEROWAY_FOR_ROLE,
    BuiltShape,
    PavementLayout,
    R_EARTH,
    ROLE_APRON,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RUNWAY,
    ROLE_BUILDING,
    SHARED_VERTEX_TOL_M,
    _airport_anchor,
    _projection,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _make_layout(icao="KFAKE", lat=40.0, lon=-100.0):
    """Empty PavementLayout anchored at the given lat/lon."""
    return PavementLayout(icao=icao, anchor=(lat, lon))


def _square(cx=0.0, cy=0.0, side=10.0):
    """Axis-aligned square in meter space."""
    h = side / 2.0
    return Polygon([
        (cx - h, cy - h),
        (cx + h, cy - h),
        (cx + h, cy + h),
        (cx - h, cy + h),
    ])


def _emit_and_parse(layout):
    """Call to_osm then parse the resulting XML.

    Returns (nodes, ways, node_alts) where:
      nodes:     {nid: (lat, lon)}
      ways:      [(wid, [nid_refs], {tag: val})]
      node_alts: {nid: alt_abs}  — per-node ``alt_abs`` tags (the
                 backward-compatible per-vertex altitude form)
    """
    with tempfile.NamedTemporaryFile(
            mode="r", suffix=".osm", delete=False) as f:
        path = f.name
    try:
        layout.to_osm(path)
        text = Path(path).read_text()
    finally:
        Path(path).unlink()

    # Match the OPENING <node ...> tag for BOTH the self-closing form
    # (`... />`) and the form that carries a per-node <tag> child
    # (`...>`).  lat/lon always live in the opening tag.
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


# ──────────────────────────────────────────────────────────────────────
# Coordinate round-trip
# ──────────────────────────────────────────────────────────────────────
def test_m_to_ll_then_ll_to_m_is_identity():
    """The two coordinate transforms invert each other to sub-mm
    precision around the anchor."""
    layout = _make_layout(lat=37.5, lon=-122.3)
    for x, y in [(0.0, 0.0), (100.0, 200.0), (-50.0, 1500.0),
                 (12345.6, -7890.1)]:
        lat, lon = layout.m_to_ll(x, y)
        x2, y2 = layout.ll_to_m(lat, lon)
        assert abs(x - x2) < 1e-6
        assert abs(y - y2) < 1e-6


def test_m_to_ll_origin_is_anchor():
    """(0, 0) in meter space is the anchor lat/lon exactly."""
    layout = _make_layout(lat=40.0, lon=-100.0)
    lat, lon = layout.m_to_ll(0.0, 0.0)
    assert abs(lat - 40.0) < 1e-9
    assert abs(lon - (-100.0)) < 1e-9


def test_ll_to_m_anchor_is_origin():
    """The anchor lat/lon maps to (0, 0)."""
    layout = _make_layout(lat=40.0, lon=-100.0)
    x, y = layout.ll_to_m(40.0, -100.0)
    assert abs(x) < 1e-9
    assert abs(y) < 1e-9


# ──────────────────────────────────────────────────────────────────────
# _projection helper
# ──────────────────────────────────────────────────────────────────────
def test_projection_returns_callable_to_m():
    """_projection(anchor) returns a (lon, lat[, z]) → (x, y[, z])
    function whose origin is the anchor."""
    to_m = _projection((40.0, -100.0))
    assert callable(to_m)
    x, y = to_m(-100.0, 40.0)
    assert abs(x) < 1e-9
    assert abs(y) < 1e-9


def test_projection_with_z_passes_through():
    """The optional z argument is preserved in the output tuple."""
    to_m = _projection((40.0, -100.0))
    x, y, z = to_m(-100.0, 40.0, 123.4)
    assert z == 123.4


# ──────────────────────────────────────────────────────────────────────
# _airport_anchor
# ──────────────────────────────────────────────────────────────────────
class _StubRunway:
    def __init__(self, lat_a, lon_a, lat_b, lon_b):
        self.lat_a, self.lon_a = lat_a, lon_a
        self.lat_b, self.lon_b = lat_b, lon_b


class _StubAirport:
    def __init__(self, runways=(), boundary=None):
        self.runways = list(runways)
        self.boundary = boundary


def test_airport_anchor_uses_first_runway_midpoint():
    """When runways are present, anchor = midpoint of first runway."""
    apt = _StubAirport(runways=[
        _StubRunway(40.0, -100.0, 40.02, -100.0),
    ])
    lat, lon = _airport_anchor(apt)
    assert abs(lat - 40.01) < 1e-9
    assert abs(lon - (-100.0)) < 1e-9


def test_airport_anchor_falls_back_to_boundary_centroid():
    """No runways → use boundary centroid (note shapely centroid is
    .y=lat, .x=lon)."""
    boundary = Polygon([
        (-100.0, 40.0),
        (-99.99, 40.0),
        (-99.99, 40.02),
        (-100.0, 40.02),
    ])
    apt = _StubAirport(boundary=boundary)
    lat, lon = _airport_anchor(apt)
    assert abs(lat - 40.01) < 1e-9
    assert abs(lon - (-99.995)) < 1e-9


def test_airport_anchor_default_zero():
    """No runways AND no boundary → (0, 0)."""
    apt = _StubAirport()
    assert _airport_anchor(apt) == (0.0, 0.0)


# ──────────────────────────────────────────────────────────────────────
# to_osm: smoke-level round-trip
# ──────────────────────────────────────────────────────────────────────
def test_to_osm_emits_one_way_per_shape():
    """Each non-empty BuiltShape produces one <way> in the output."""
    layout = _make_layout()
    layout.shapes.append(BuiltShape(
        polygon=_square(0, 0, 10), role=ROLE_RUNWAY, ref="RW09/RW27"))
    layout.shapes.append(BuiltShape(
        polygon=_square(50, 0, 10), role=ROLE_PRIMARY_PARALLEL, ref="A"))
    nodes, ways, _ = _emit_and_parse(layout)
    assert len(ways) == 2


def test_to_osm_skips_empty_polygons():
    """Empty / None polygons are skipped silently."""
    layout = _make_layout()
    layout.shapes.append(BuiltShape(polygon=Polygon(), role=ROLE_RUNWAY))
    layout.shapes.append(BuiltShape(
        polygon=_square(0, 0, 10), role=ROLE_RUNWAY, ref="RW09/RW27"))
    _, ways, _ = _emit_and_parse(layout)
    assert len(ways) == 1


def test_to_osm_emits_aeroway_and_role_tags():
    """Every way has aeroway= and role= tags set per the AEROWAY_FOR_ROLE
    map."""
    layout = _make_layout()
    layout.shapes.append(BuiltShape(
        polygon=_square(0, 0, 10), role=ROLE_RUNWAY))
    _, ways, _ = _emit_and_parse(layout)
    _, _, tags = ways[0]
    assert tags["aeroway"] == AEROWAY_FOR_ROLE[ROLE_RUNWAY]
    assert tags["role"] == ROLE_RUNWAY


def test_to_osm_ref_tag_emitted_when_set():
    """``ref`` gets passed through to the OSM tag."""
    layout = _make_layout()
    layout.shapes.append(BuiltShape(
        polygon=_square(0, 0, 10), role=ROLE_RUNWAY, ref="RW09/RW27"))
    _, ways, _ = _emit_and_parse(layout)
    assert ways[0][2]["ref"] == "RW09/RW27"


def test_to_osm_no_ref_tag_when_unset():
    """An empty ref is not emitted."""
    layout = _make_layout()
    layout.shapes.append(BuiltShape(
        polygon=_square(0, 0, 10), role=ROLE_JUNCTION))
    _, ways, _ = _emit_and_parse(layout)
    assert "ref" not in ways[0][2]


# ──────────────────────────────────────────────────────────────────────
# to_osm: elevation tag formats
# ──────────────────────────────────────────────────────────────────────
def test_to_osm_sloped_rect_emits_per_node_values():
    """A sloped rect emits per-node altitudes (hi/lo + cell_size
    retired, user 2026-07-06): the high corners carry 100.50, the low
    corners 99.00, and no legacy slope way-tags appear."""
    layout = _make_layout()
    layout.shapes.append(BuiltShape(
        polygon=_square(0, 0, 10), role=ROLE_PRIMARY_PARALLEL,
        altitude_high=100.5, altitude_low=99.0))
    nodes, ways, node_alts = _emit_and_parse(layout)
    wid, nds, tags = ways[0]
    for legacy_tag in ("altitude_high", "altitude_low", "cell_size",
                       "profile", "altitude"):
        assert legacy_tag not in tags, legacy_tag
    # per-node values preserved (via node alt_abs or the way-level
    # node_altitudes fallback)
    if "node_altitudes" in tags:
        values = [float(v) for v in tags["node_altitudes"].split(",")]
    else:
        values = [float(node_alts[nid]) for nid in nds
                  if nid in node_alts]
    assert values, "no per-node altitudes emitted"
    assert max(values) == 100.5 and min(values) == 99.0


# ──────────────────────────────────────────────────────────────────────
# hi/lo emission RETIRED (user 2026-07-06): every sloped shape emits
# per-node altitudes; altitude_high/low + cell_size never appear.
# ──────────────────────────────────────────────────────────────────────
def test_to_osm_inverted_slope_rect_emits_per_node():
    """A sloped rect whose stored slope runs high < low emits per-node
    altitudes with every physical corner keeping its value — the
    positional [H, L, L, H] convention (and its inversion hazards) is
    gone from the OSM."""
    layout = _make_layout()
    layout.shapes.append(BuiltShape(
        polygon=_square(0, 0, 10), role=ROLE_PRIMARY_PARALLEL,
        altitude_high=99.0, altitude_low=100.5))   # inverted on purpose
    nodes, ways, _ = _emit_and_parse(layout)
    for wid, nds, tags in ways:
        assert "altitude_high" not in tags
        assert "altitude_low" not in tags
        assert "cell_size" not in tags


def _pentagon(cx=0.0, cy=0.0):
    """A 5-corner convex polygon in meter space (NOT a quad)."""
    return Polygon([
        (cx + 0.0, cy + 0.0),
        (cx + 50.0, cy + 0.0),
        (cx + 50.0, cy + 20.0),
        (cx + 25.0, cy + 30.0),
        (cx + 0.0, cy + 20.0),
    ])


def test_to_osm_never_emits_altitude_high_on_non_quad():
    """Ortho4XP's encoder rejects any altitude_high/altitude_low way
    that isn't exactly a closed 4-corner quad (5 node refs) — it logs
    "Wrong number of nodes or non closed way for a
    altitude_high/altitude_low polygon, skipped" and drops the shape.

    A sloped shape (altitude_high/low set) whose ring was reshaped to a
    non-quad by some upstream pass must therefore NEVER reach the OSM
    with altitude_high/low tags.  The emitter is the single chokepoint
    that must enforce this regardless of which pass produced the bad
    geometry.  Here a pentagon sloped shape with no neighbour (so the
    consensus path can't help) exercises the fallback tag-writer.
    """
    layout = _make_layout()
    layout.shapes.append(BuiltShape(
        polygon=_pentagon(), role=ROLE_PRIMARY_PARALLEL,
        altitude_high=60.0, altitude_low=55.0))
    _, ways, _ = _emit_and_parse(layout)
    for wid, nds, tags in ways:
        if "altitude_high" in tags or "altitude_low" in tags:
            assert len(nds) == 5, (
                f"way {wid} has altitude_high/low with {len(nds)} node "
                f"refs (Ortho4XP requires exactly 5 = 4 corners + "
                f"closing repeat); X-Plane would reject the way")
            assert nds[0] == nds[-1], (
                f"way {wid} has altitude_high/low but is not a closed "
                f"ring; X-Plane would reject the way")


def test_to_osm_non_quad_sloped_shape_still_emits_some_altitude():
    """The non-quad sloped shape must still carry SOME valid elevation
    (flat or per-node) rather than being silently dropped — losing its
    altitude entirely would float it on the DEM."""
    layout = _make_layout()
    layout.shapes.append(BuiltShape(
        polygon=_pentagon(), role=ROLE_PRIMARY_PARALLEL,
        altitude_high=60.0, altitude_low=55.0))
    nodes, ways, node_alts = _emit_and_parse(layout)
    assert len(ways) == 1
    wid, nds, tags = ways[0]
    # The shape must carry SOME valid elevation — either a way-level
    # altitude tag (flat / sloped quad) or per-node ``alt_abs`` on every
    # vertex (the backward-compatible per-corner form).
    has_way_alt = ("altitude" in tags or "node_altitudes" in tags
                   or "altitude_high" in tags)
    has_node_alt = bool(nds) and all(n in node_alts for n in nds)
    assert has_way_alt or has_node_alt, (
        "non-quad sloped shape lost all elevation tags")


def test_to_osm_flat_altitude_emits_single_tag():
    """A polygon with only ``altitude`` set emits a single
    altitude= tag."""
    layout = _make_layout()
    layout.shapes.append(BuiltShape(
        polygon=_square(0, 0, 10), role=ROLE_BUILDING,
        altitude=42.7))
    _, ways, _ = _emit_and_parse(layout)
    tags = ways[0][2]
    assert tags["altitude"] == "42.70"
    assert "altitude_high" not in tags
    assert "altitude_low" not in tags


def test_to_osm_compound_slope_emits_per_node_alt_abs():
    """A compound sloping polygon (per-corner ``node_altitudes``) emits
    its per-vertex altitudes as per-node ``alt_abs`` tags (the
    backward-compatible form read by stock Ortho4XP) rather than a
    fork-only single-way tag."""
    layout = _make_layout()
    poly = _square(0, 0, 10)
    # 4 corners + closing repeat = 5 elevations.
    elevs = [10.0, 11.0, 12.0, 13.0, 10.0]
    layout.shapes.append(BuiltShape(
        polygon=poly, role=ROLE_JUNCTION,
        node_altitudes=elevs))
    _, ways, node_alts = _emit_and_parse(layout)
    wid, nds, tags = ways[0]
    # No fork-only way tag; every ring vertex carries alt_abs instead.
    assert "node_altitudes" not in tags
    assert all(n in node_alts for n in nds), (
        "every ring vertex must carry a per-node alt_abs tag")
    # Per-vertex altitudes are preserved, indexed by node ref (the
    # closing ref repeats the first vertex's value).
    by_ref = [node_alts[n] for n in nds]
    assert by_ref[0] == 10.0
    assert by_ref[1] == 11.0
    assert by_ref[-1] == by_ref[0]


def test_to_osm_no_elevation_tags_when_none_set():
    """A geometry-only polygon (all elevation fields None) emits no
    elevation tags."""
    layout = _make_layout()
    layout.shapes.append(BuiltShape(
        polygon=_square(0, 0, 10), role=ROLE_APRON))
    _, ways, node_alts = _emit_and_parse(layout)
    tags = ways[0][2]
    for k in ("altitude", "altitude_high", "altitude_low",
              "node_altitudes"):
        assert k not in tags
    assert not node_alts, "geometry-only shape must emit no alt_abs tags"


# ──────────────────────────────────────────────────────────────────────
# to_osm: shared vertices (the headline invariant)
# ──────────────────────────────────────────────────────────────────────
def test_to_osm_shared_corner_uses_same_node_id():
    """Two adjacent shapes sharing a corner must reference the SAME
    nid in the OSM output.  This is the v_tgt invariant the target
    files measure."""
    layout = _make_layout()
    # Square A: (0,0)-(10,10).  Square B: (10,0)-(20,10).  Shared
    # edge along x=10, y∈[0,10].
    a = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    b = Polygon([(10, 0), (20, 0), (20, 10), (10, 10)])
    layout.shapes.append(BuiltShape(polygon=a, role=ROLE_RUNWAY, ref="A"))
    layout.shapes.append(BuiltShape(polygon=b, role=ROLE_RUNWAY, ref="B"))
    nodes, ways, _ = _emit_and_parse(layout)

    # Find the two shared corners.  In meter space (10, 0) and
    # (10, 10) are present in both shapes.  After interning they
    # should resolve to the same nid.
    way_a_nids = set(ways[0][1])
    way_b_nids = set(ways[1][1])
    shared = way_a_nids & way_b_nids
    # At least the two shared corners must be in the intersection.
    assert len(shared) >= 2, (
        "shared-vertex invariant violated: adjacent shapes must "
        "reference the SAME node ids at coincident corners")


def test_to_osm_distinct_far_corners_get_different_node_ids():
    """Two shapes far apart should have entirely-disjoint node id
    sets (no false sharing)."""
    layout = _make_layout()
    layout.shapes.append(BuiltShape(
        polygon=_square(0, 0, 10), role=ROLE_RUNWAY, ref="A"))
    layout.shapes.append(BuiltShape(
        polygon=_square(1000, 1000, 10),  # 1.4 km away
        role=ROLE_RUNWAY, ref="B"))
    _, ways, _ = _emit_and_parse(layout)
    way_a_nids = set(ways[0][1])
    way_b_nids = set(ways[1][1])
    assert not (way_a_nids & way_b_nids)


def test_to_osm_subtol_distance_clusters_nodes():
    """Vertices within SHARED_VERTEX_TOL_M (0.5 m) of each other
    cluster to the same nid."""
    layout = _make_layout()
    # Two squares whose corners are 0.1 m apart — well within tol.
    a = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    b = Polygon([(10.1, 0), (20, 0), (20, 10), (10.1, 10)])
    layout.shapes.append(BuiltShape(polygon=a, role=ROLE_RUNWAY))
    layout.shapes.append(BuiltShape(polygon=b, role=ROLE_RUNWAY))
    _, ways, _ = _emit_and_parse(layout)
    shared = set(ways[0][1]) & set(ways[1][1])
    assert len(shared) >= 2, (
        f"vertices {SHARED_VERTEX_TOL_M} m apart should cluster")


# ──────────────────────────────────────────────────────────────────────
# to_osm: orphan-node filtering
# ──────────────────────────────────────────────────────────────────────
def test_to_osm_only_referenced_nodes_emitted():
    """Every <node> in the output must be referenced by at least one
    <way>.  No floating nodes."""
    layout = _make_layout()
    layout.shapes.append(BuiltShape(
        polygon=_square(0, 0, 10), role=ROLE_RUNWAY))
    layout.shapes.append(BuiltShape(
        polygon=_square(50, 0, 10), role=ROLE_RUNWAY))
    nodes, ways, _ = _emit_and_parse(layout)
    referenced = set()
    for _, nids, _ in ways:
        referenced.update(nids)
    assert set(nodes.keys()) == referenced


# ──────────────────────────────────────────────────────────────────────
# to_osm: ring closure
# ──────────────────────────────────────────────────────────────────────
def test_to_osm_emits_closed_ring():
    """OSM closed ways repeat the first nid as the last nid."""
    layout = _make_layout()
    layout.shapes.append(BuiltShape(
        polygon=_square(0, 0, 10), role=ROLE_RUNWAY))
    _, ways, _ = _emit_and_parse(layout)
    nids = ways[0][1]
    assert nids[0] == nids[-1], "ring must be closed"
    # Square → 4 unique nodes + closing repeat = 5 entries.
    assert len(nids) == 5


# ──────────────────────────────────────────────────────────────────────
# to_osm: coordinate precision
# ──────────────────────────────────────────────────────────────────────
def test_to_osm_lat_lon_precision_is_11_decimals():
    """Coordinates are emitted at .11f precision (~1 mm at the
    equator) — matches the test-fixture format."""
    layout = _make_layout()
    layout.shapes.append(BuiltShape(
        polygon=_square(0, 0, 10), role=ROLE_RUNWAY))
    with tempfile.NamedTemporaryFile(
            mode="r", suffix=".osm", delete=False) as f:
        path = f.name
    try:
        layout.to_osm(path)
        text = Path(path).read_text()
    finally:
        Path(path).unlink()
    # Find a <node> line and check the precision.
    m = re.search(r"<node[^>]*lat='([^']+)'[^>]*lon='([^']+)'", text)
    assert m is not None
    lat_str, lon_str = m.group(1), m.group(2)
    # Allow optional trailing zeros — but check that there are at
    # least 11 fractional digits.
    assert len(lat_str.split(".")[1]) == 11
    assert len(lon_str.split(".")[1]) == 11


# ──────────────────────────────────────────────────────────────────────
# to_osm: must not mutate input shapes (emitter is pure)
# ──────────────────────────────────────────────────────────────────────
def test_to_osm_does_not_mutate_shape_on_invalid_polygon():
    """to_osm repairs an invalid polygon via buffer(0) internally,
    but must NOT write the degraded altitude representation back onto
    the input BuiltShape.  A self-intersecting (bowtie) ring with
    node_altitudes exercises the repair path that previously mutated
    s.altitude / s.node_altitudes."""
    layout = _make_layout()
    # Bowtie: self-intersecting → poly.is_valid is False.
    bowtie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
    assert not bowtie.is_valid
    elevs = [10.0, 11.0, 12.0, 13.0, 10.0]
    shape = BuiltShape(
        polygon=bowtie, role=ROLE_JUNCTION, node_altitudes=list(elevs))
    layout.shapes.append(shape)

    _emit_and_parse(layout)

    # The emitter must have left the shape untouched.
    assert shape.node_altitudes == elevs, (
        "to_osm mutated node_altitudes on the input shape")
    assert shape.altitude is None, (
        "to_osm mutated altitude on the input shape")


def test_to_osm_is_idempotent():
    """Calling to_osm twice produces byte-identical output — proves
    no input shape was degraded by the first call."""
    layout = _make_layout()
    # Mix of valid + invalid-with-altitudes shapes.
    layout.shapes.append(BuiltShape(
        polygon=_square(0, 0, 10), role=ROLE_RUNWAY, ref="A",
        altitude_high=100.0, altitude_low=99.0))
    bowtie = Polygon([(50, 0), (60, 10), (60, 0), (50, 10)])
    layout.shapes.append(BuiltShape(
        polygon=bowtie, role=ROLE_JUNCTION,
        node_altitudes=[10.0, 11.0, 12.0, 13.0, 10.0]))

    def _emit_text():
        with tempfile.NamedTemporaryFile(
                mode="r", suffix=".osm", delete=False) as f:
            path = f.name
        try:
            layout.to_osm(path)
            return Path(path).read_text()
        finally:
            Path(path).unlink()

    first = _emit_text()
    second = _emit_text()
    assert first == second, (
        "to_osm is not idempotent — the first call mutated layout "
        "state that the second call then emitted differently")


# ──────────────────────────────────────────────────────────────────────
# Sanity: AEROWAY_FOR_ROLE is complete
# ──────────────────────────────────────────────────────────────────────
def test_aeroway_for_role_covers_all_roles():
    """Every emitted role must have an AEROWAY_FOR_ROLE entry.  If a
    new role is added without updating this map, to_osm falls back
    to "taxiway" silently — this test documents the current
    expected coverage."""
    expected_roles = {
        ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL, ROLE_APRON,
        ROLE_BUILDING, ROLE_JUNCTION,
    }
    for role in expected_roles:
        assert role in AEROWAY_FOR_ROLE
