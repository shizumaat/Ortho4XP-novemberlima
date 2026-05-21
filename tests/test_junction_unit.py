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
  * ``_split_narrow_necks`` — splits a junction that is narrow by a
    single MRR criterion (short-side OR ratio).
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
    _split_narrow_necks,
    longest_runway_axis_deg,
)
from auto_patch.layout import (
    BuiltShape,
    PavementLayout,
    ROLE_JUNCTION,
    ROLE_RUNWAY,
)


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


# ── _split_narrow_necks ───────────────────────────────────────────


def test_split_narrow_neck_on_single_criterion():
    """A junction narrow by RATIO only (short-side ≥ NECK_ABSOLUTE_M
    but short/long < NECK_RELATIVE) must still be split into two
    junctions.  The split predicate is "narrow on EITHER criterion",
    so tightening it to require BOTH (an ``and``→``or`` regression in
    the skip-guard) would leave this junction unsplit.

    Geometry: a 6 m × 80 m strip (short=6 ≥ 5.0, ratio=0.075 < 0.10).
    Cut perpendicular to a north-south runway axis (0°) bisects the
    long dimension into two 6 m × 40 m pieces (240 m² each, well
    above the 50 m² keep threshold).
    """
    neck = BuiltShape(polygon=_rect(0.0, 0.0, 80.0, 6.0),
                      role=ROLE_JUNCTION)
    layout = _layout(neck)
    _split_narrow_necks(layout, runway_axis_deg=0.0)
    junctions = [s for s in layout.shapes if s.role == ROLE_JUNCTION]
    assert len(junctions) == 2
    for s in junctions:
        assert s.polygon.area >= 50.0


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
