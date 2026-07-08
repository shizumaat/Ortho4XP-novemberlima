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
