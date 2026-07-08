"""Unit tests for junction-pass helpers, exercised with synthetic
geometry (no airport build / X-Plane install required).

These complement the airport-level regression tests in
``test_junction_rules.py`` / ``test_junction_invariants.py`` by
pinning the behaviour of individual passes directly, so a bug in
one pass surfaces at its own location instead of as a downstream
geometric-invariant failure (or, worse, as a *skipped* test).

Covered:
  * ``longest_runway_axis_deg`` — picks the LONGEST runway's axis,
    returns ``None`` only when no runway is present.
  * ``_merge_sliver_junctions_into_neighbours`` — requires a shared
    EDGE (≥ 2 shared vertices), not a single shared point.
"""
from __future__ import annotations

import math

from shapely.geometry import Polygon

# ``junction_repair`` imports ``elevation`` which imports back from
# ``junction_repair`` — importing elevation first establishes the
# correct module-init order and avoids the partial-init circular
# import (see memory: junction_repair ↔ elevation cycle).
import auto_patch.elevation  # noqa: F401
from auto_patch.junction_repair import (
    _merge_sliver_junctions_into_neighbours,
)
from auto_patch.junction_rules import (
    longest_runway_axis_deg,
)
from auto_patch.layout import (
    BuiltShape,
    PavementLayout,
    ROLE_JUNCTION,
    ROLE_RUNWAY,
)
from auto_patch.pavement.vertices import _enforce_shared_vertices


def _rect(x0: float, y0: float, x1: float, y1: float) -> Polygon:
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def _layout(*shapes: BuiltShape) -> PavementLayout:
    return PavementLayout(icao="TEST", anchor=(0.0, 0.0),
                          shapes=list(shapes))


# ── longest_runway_axis_deg ───────────────────────────────────────


def test_longest_runway_axis_uses_the_longest_runway():
    """The axis must come from the LONGEST runway, not the shortest
    (and not be nullified).  A long east-west runway (axis 90°) plus
    a shorter north-south one (axis 0°) must yield 90°.
    """
    long_ew = BuiltShape(polygon=_rect(0.0, 0.0, 100.0, 10.0),
                         role=ROLE_RUNWAY)
    short_ns = BuiltShape(polygon=_rect(0.0, 0.0, 8.0, 40.0),
                          role=ROLE_RUNWAY)
    axis = longest_runway_axis_deg(_layout(long_ew, short_ns))
    assert axis is not None
    # 0° = +Y (north); an east-west runway's long axis is 90°.
    assert abs(axis - 90.0) < 1e-6


def test_longest_runway_axis_none_without_runway():
    """No runway shape → axis is undefined (``None``)."""
    junction = BuiltShape(polygon=_rect(0.0, 0.0, 50.0, 50.0),
                          role=ROLE_JUNCTION)
    assert longest_runway_axis_deg(_layout(junction)) is None


# (session 51) `_split_narrow_necks` unit tests REMOVED — the function
# was retired in favour of `pavement/apron_necks.py::split_polygon_at_necks`
# (session-50 medial-axis traced neck splitter, called pre-decompose).


# ── _merge_sliver_junctions_into_neighbours ───────────────────────


def test_sliver_merge_requires_shared_edge_not_point():
    """A sliver junction touching a large junction at a SINGLE vertex
    must NOT be merged — adjacency requires a shared edge (≥ 2 shared
    vertices).  Loosening that to a single shared point would drop
    the point-touching sliver (it shares only the corner) and produce
    a non-edge merge.
    """
    big = BuiltShape(polygon=_rect(0.0, 0.0, 100.0, 100.0),
                     role=ROLE_JUNCTION)
    # Triangle touching ``big`` only at the corner (100, 100).
    point_sliver = BuiltShape(
        polygon=Polygon([(100.0, 100.0), (110.0, 105.0),
                         (105.0, 110.0)]),
        role=ROLE_JUNCTION)
    layout = _layout(big, point_sliver)
    merged = _merge_sliver_junctions_into_neighbours(layout)
    assert merged == 0
    assert len(layout.shapes) == 2


def test_sliver_merge_absorbs_edge_adjacent_sliver():
    """A sliver sharing a full edge (two corner vertices) with a much
    larger junction IS merged into it, leaving a single shape.
    """
    big = BuiltShape(polygon=_rect(0.0, 0.0, 100.0, 100.0),
                     role=ROLE_JUNCTION)
    # 4 m × 100 m strip sharing the right edge corners (100, 0) and
    # (100, 100): area 400 m² < 1000 sliver cap, ratio 0.04 < 0.05.
    edge_sliver = BuiltShape(polygon=_rect(100.0, 0.0, 104.0, 100.0),
                             role=ROLE_JUNCTION)
    layout = _layout(big, edge_sliver)
    merged = _merge_sliver_junctions_into_neighbours(layout)
    assert merged == 1
    assert len(layout.shapes) == 1
    # The surviving shape spans the union of both rects.
    minx, miny, maxx, maxy = layout.shapes[0].polygon.bounds
    assert math.isclose(maxx, 104.0, abs_tol=1e-6)


# ── _enforce_shared_vertices: runway = geometry authority (R1) ──────
#
# 2026-07-08 formation diagnosis: the raw cluster MEAN detached runway
# frontages from the runway contour (junction frontage chains cluster
# among themselves; the mean lands 0.014-0.27 m off the runway edge —
# the epsilon-wedge / sliver-overlap / mixed-value classes at KCLT 18L
# and SPJC 16L, gate-on AND gate-off).  Three rules:
#   1. cluster holds a runway vertex → canonical point IS that runway
#      vertex (runway never moves); disagreeing runway authorities →
#      whole cluster unmoved;
#   2. runway-free cluster whose mean is within tol of a runway
#      boundary → mean projects onto the boundary;
#   3. anything else → plain mean (legacy behavior).


def _exterior(shape: BuiltShape) -> set:
    return {(round(x, 6), round(y, 6))
            for x, y in shape.polygon.exterior.coords}


def test_cluster_with_runway_vertex_snaps_to_it():
    runway = BuiltShape(polygon=_rect(0.0, 0.0, 100.0, 30.0),
                        role=ROLE_RUNWAY)
    junction_a = BuiltShape(
        polygon=Polygon([(0.0, -0.4), (10.0, -0.4),
                         (10.0, -8.0), (0.0, -8.0)]),
        role=ROLE_JUNCTION)
    junction_b = BuiltShape(
        polygon=Polygon([(1.0, -0.4), (1.0, -8.0),
                         (-6.0, -8.0), (-6.0, -0.6)]),
        role=ROLE_JUNCTION)
    layout = _layout(runway, junction_a, junction_b)

    _enforce_shared_vertices(layout, tol=1.5)

    # Both junction frontage vertices land ON the runway corner …
    assert (0.0, 0.0) in _exterior(junction_a)
    assert (0.0, -0.4) not in _exterior(junction_a)
    assert (0.0, 0.0) in _exterior(junction_b)
    assert (1.0, -0.4) not in _exterior(junction_b)
    # … and the runway itself is untouched (geometry authority).
    assert _exterior(runway) >= {(0.0, 0.0), (100.0, 0.0),
                                 (100.0, 30.0), (0.0, 30.0)}


def test_disagreeing_runway_authorities_leave_cluster_unmoved():
    runway_west = BuiltShape(polygon=_rect(0.0, 0.0, 100.0, 30.0),
                             role=ROLE_RUNWAY)
    runway_east = BuiltShape(polygon=_rect(100.5, 0.0, 200.0, 30.0),
                             role=ROLE_RUNWAY)
    junction = BuiltShape(
        polygon=Polygon([(100.2, -0.3), (110.0, -0.3),
                         (110.0, -8.0), (100.2, -8.0)]),
        role=ROLE_JUNCTION)
    layout = _layout(runway_west, runway_east, junction)

    _enforce_shared_vertices(layout, tol=1.5)

    # The (100,0) / (100.5,0) / (100.2,-0.3) cluster holds vertices of
    # TWO runway shapes at materially different positions — nothing in
    # it may move (never average two authorities).
    assert (100.0, 0.0) in _exterior(runway_west)
    assert (100.5, 0.0) in _exterior(runway_east)
    assert (100.2, -0.3) in _exterior(junction)


def test_runway_free_cluster_projects_onto_runway_boundary():
    runway = BuiltShape(polygon=_rect(0.0, 0.0, 100.0, 30.0),
                        role=ROLE_RUNWAY)
    # Two frontage vertices mid-frontage (no runway vertex nearby);
    # mean (40.3, -0.2) sits 0.2 m off the y=0 runway edge.
    junction_a = BuiltShape(
        polygon=Polygon([(40.0, -0.2), (50.0, -6.0), (30.0, -6.0)]),
        role=ROLE_JUNCTION)
    junction_b = BuiltShape(
        polygon=Polygon([(40.6, -0.2), (55.0, -8.0), (58.0, -2.0)]),
        role=ROLE_JUNCTION)
    layout = _layout(runway, junction_a, junction_b)

    _enforce_shared_vertices(layout, tol=1.5)

    assert (40.3, 0.0) in _exterior(junction_a)
    assert (40.3, 0.0) in _exterior(junction_b)
    assert (40.0, -0.2) not in _exterior(junction_a)
    assert (40.6, -0.2) not in _exterior(junction_b)


def test_cluster_far_from_runway_keeps_plain_mean():
    runway = BuiltShape(polygon=_rect(0.0, 0.0, 100.0, 30.0),
                        role=ROLE_RUNWAY)
    junction_a = BuiltShape(
        polygon=Polygon([(300.0, -50.0), (310.0, -50.0),
                         (310.0, -58.0), (300.0, -58.0)]),
        role=ROLE_JUNCTION)
    junction_b = BuiltShape(
        polygon=Polygon([(301.0, -50.0), (301.0, -58.0),
                         (294.0, -58.0), (294.0, -50.6)]),
        role=ROLE_JUNCTION)
    layout = _layout(runway, junction_a, junction_b)

    _enforce_shared_vertices(layout, tol=1.5)

    # Legacy behavior: the (300,-50)/(301,-50) cluster collapses to
    # its mean (300.5, -50).
    assert (300.5, -50.0) in _exterior(junction_a)
    assert (300.5, -50.0) in _exterior(junction_b)


# ── _enforce_runway_1to1_sharing: off-source carve vs shapely-2
#    GeometryCollection (KCLT #336) ────────────────────────────────
#
# 2026-07-08 diagnosis: the runway-frontage straightening chord can
# sweep in a large OFF-SOURCE region (grass along the frontage); the
# carve subtracts it back out, but shapely-2 difference() can return a
# GeometryCollection (polygonal parts + line/point crumbs where the
# carve boundary is tangent to the operands).  The split-keep branch
# only understood MultiPolygon, so a collection fell through to
# ``_carved_ok = False`` and the FALLBACK kept the whole gain — KCLT
# 18L: junction #336, ~17 k m² of grass emitted as a 24.7 k m²
# junction 31 % on source.  ``_polygonal_parts`` (same reduction as
# the global-slice / CYUL GeometryCollection fixes) now runs first.


def test_polygonal_parts_reduces_geometry_collection():
    """A GeometryCollection of polygons + line/point crumbs reduces to
    its polygonal union; non-collections pass through unchanged."""
    from shapely.geometry import (
        GeometryCollection, LineString, MultiPolygon, Point,
    )

    from auto_patch.junction_rules import _polygonal_parts

    piece_a = _rect(0.0, 0.0, 10.0, 10.0)
    piece_b = _rect(20.0, 0.0, 30.0, 10.0)
    collection = GeometryCollection([
        piece_a, piece_b,
        LineString([(50.0, 0.0), (51.0, 0.0)]),   # tangency crumb
        Point(60.0, 0.0),
    ])
    reduced = _polygonal_parts(collection)
    assert reduced.geom_type in ("Polygon", "MultiPolygon")
    assert math.isclose(reduced.area, piece_a.area + piece_b.area)

    untouched = _rect(0.0, 0.0, 5.0, 5.0)
    assert _polygonal_parts(untouched) is untouched
    multi = MultiPolygon([piece_a, piece_b])
    assert _polygonal_parts(multi) is multi


def test_runway_rewrite_carves_off_source_gain_through_collection(
        monkeypatch):
    """Synthetic KCLT-#336: the frontage rewrite's straightening chord
    sweeps two >500 m² grass triangles; every polygonal difference()
    result is wrapped into a GeometryCollection (the shapely-2 tangency
    behaviour observed in production), and the carve must STILL remove
    the off-source gain instead of falling back uncarved.
    """
    from shapely.geometry import GeometryCollection, LineString, Point
    from shapely.geometry.base import BaseGeometry

    from auto_patch.junction_rules import _enforce_runway_1to1_sharing

    real_difference = BaseGeometry.difference

    def crumbed_difference(self, other, **kwargs):
        result = real_difference(self, other, **kwargs)
        if result.geom_type in ("Polygon", "MultiPolygon") \
                and not result.is_empty:
            parts = list(getattr(result, "geoms", [result]))
            # A far-away hairline crumb: zero area, polygonal filters
            # must ignore it — exactly the production tangency crumbs.
            return GeometryCollection(
                parts + [LineString([(-900.0, -900.0),
                                     (-899.0, -900.0)])])
        return result

    monkeypatch.setattr(BaseGeometry, "difference", crumbed_difference)

    runway = BuiltShape(polygon=_rect(0.0, 0.0, 200.0, 45.0),
                        role=ROLE_RUNWAY)
    # Junction strip hugging the runway frontage x∈[60,140]: the five
    # y=45 vertices form ONE runway-adjacent run, replaced by the two
    # runway corners (0,45)/(200,45) — the chord then sweeps two
    # 750 m² off-source triangles (x∈[0,60] and x∈[140,200]).
    junction = BuiltShape(
        polygon=Polygon([(60.0, 45.0), (80.0, 45.0), (100.0, 45.0),
                         (120.0, 45.0), (140.0, 45.0),
                         (140.0, 70.0), (60.0, 70.0)]),
        role=ROLE_JUNCTION)
    layout = _layout(runway, junction)
    layout.runway_union = runway.polygon
    # Source pavement = exactly the junction body (the grass beyond it
    # carries no source pavement).
    layout.source_pavement_union = junction.polygon

    _enforce_runway_1to1_sharing(layout)

    carved = junction.polygon
    assert carved.geom_type == "Polygon"
    # The original on-source body is kept …
    assert carved.covers(Point(100.0, 60.0))
    # … and the swept grass triangles are carved back out (the
    # uncarved fallback would keep both probe points).
    assert not carved.covers(Point(30.0, 60.0))
    assert not carved.covers(Point(170.0, 60.0))
    # Majority of the carved shape rests on source pavement (only the
    # runway-side halo strip may remain off-source).
    on_source = carved.intersection(
        layout.source_pavement_union).area / carved.area
    assert on_source > 0.6


# ── _drop_off_source_residue: near-zero drop vs the route-proximity
#    exemption (KCLT 18R-end cluster) ───────────────────────────────
#
# 2026-07-08 diagnosis: pieces minted by the apron route-proximity CUT
# carry ``from_route_proximity_cut`` and were exempted from the whole
# off-source residue drop — including its near-zero branch, so five
# 74-498 m² 0 %-on-source grass pieces at KCLT's 18R end emitted as
# apron/junction pavement.  ORDERING CONSTRAINT: a ~0 %-on-source
# fragment is phantom whatever its provenance, so the near-zero drop
# is judged BEFORE the exemption; partial-coverage cut pieces above
# the floor keep the exemption (they are re-partitions of pavement
# their PARENT legitimately kept).


def test_off_source_drop_near_zero_beats_route_proximity_exemption():
    from auto_patch.junction_repair import _drop_off_source_residue

    source = _rect(0.0, 0.0, 100.0, 100.0)
    # 400 m² cut piece with ZERO source pavement under it → phantom,
    # dropped despite the route-proximity flag.
    phantom = BuiltShape(polygon=_rect(200.0, 200.0, 220.0, 220.0),
                         role=ROLE_JUNCTION,
                         from_route_proximity_cut=True)
    # 400 m² cut piece 35 % on source (x∈[93,100] band): below the 50 %
    # size-capped threshold, but a deliberate re-partition of kept
    # pavement — the exemption must preserve it (Fix C territory).
    partial = BuiltShape(polygon=_rect(93.0, 0.0, 113.0, 20.0),
                         role=ROLE_JUNCTION,
                         from_route_proximity_cut=True)
    layout = _layout(phantom, partial)
    layout.source_pavement_union = source

    dropped = _drop_off_source_residue(layout)

    assert dropped == 1
    assert len(layout.shapes) == 1
    assert layout.shapes[0] is partial
