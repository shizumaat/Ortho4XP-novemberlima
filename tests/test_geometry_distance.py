"""Property-based tests for the junction_rules point/segment distance
helpers.

``_point_segment_distance(p, a, b)`` returns ``(distance, foot_x,
foot_y)`` — the distance from p to segment a→b and the closest point on
it.  ``_point_perp_dist_within_segment(p, a, b)`` returns the
perpendicular distance to the LINE only when the foot lies strictly
inside the segment, else ``None``.
"""
from __future__ import annotations

import math

from hypothesis import given

from strategies import geo_point, segment

from auto_patch.junction_rules import (
    _point_perp_dist_within_segment,
    _point_segment_distance,
)

_ABS = 1e-4   # absolute slack for ~1e4-magnitude coordinate arithmetic


class TestPointSegmentDistance:
    """Properties of _point_segment_distance."""

    @given(p=geo_point, seg=segment)
    def test_non_negative(self, p, seg):
        a, b = seg
        d, _, _ = _point_segment_distance(p[0], p[1], a[0], a[1], b[0], b[1])
        assert d >= 0.0

    @given(p=geo_point, seg=segment)
    def test_not_greater_than_endpoint_distances(self, p, seg):
        # The segment is at least as close as either of its endpoints.
        a, b = seg
        d, _, _ = _point_segment_distance(p[0], p[1], a[0], a[1], b[0], b[1])
        da = math.hypot(p[0] - a[0], p[1] - a[1])
        db = math.hypot(p[0] - b[0], p[1] - b[1])
        assert d <= min(da, db) + _ABS

    @given(p=geo_point, seg=segment)
    def test_distance_equals_dist_to_foot(self, p, seg):
        # The returned distance is exactly the distance to the returned
        # foot point.
        a, b = seg
        d, fx, fy = _point_segment_distance(
            p[0], p[1], a[0], a[1], b[0], b[1])
        assert math.isclose(
            d, math.hypot(p[0] - fx, p[1] - fy),
            rel_tol=1e-9, abs_tol=_ABS)

    @given(p=geo_point, seg=segment)
    def test_foot_lies_on_segment(self, p, seg):
        # |a-foot| + |foot-b| == |a-b| iff foot is on segment [a, b].
        a, b = seg
        _, fx, fy = _point_segment_distance(
            p[0], p[1], a[0], a[1], b[0], b[1])
        af = math.hypot(fx - a[0], fy - a[1])
        fb = math.hypot(b[0] - fx, b[1] - fy)
        ab = math.hypot(b[0] - a[0], b[1] - a[1])
        assert math.isclose(af + fb, ab, rel_tol=1e-9, abs_tol=_ABS)

    @given(p=geo_point, seg=segment)
    def test_symmetric_in_endpoints(self, p, seg):
        # Distance to a→b equals distance to b→a (and the same foot).
        a, b = seg
        d1, fx1, fy1 = _point_segment_distance(
            p[0], p[1], a[0], a[1], b[0], b[1])
        d2, fx2, fy2 = _point_segment_distance(
            p[0], p[1], b[0], b[1], a[0], a[1])
        assert math.isclose(d1, d2, rel_tol=1e-9, abs_tol=_ABS)
        assert math.isclose(fx1, fx2, rel_tol=1e-9, abs_tol=_ABS)
        assert math.isclose(fy1, fy2, rel_tol=1e-9, abs_tol=_ABS)

    @given(p=geo_point, a=geo_point)
    def test_degenerate_segment_is_point_distance(self, p, a):
        # A zero-length segment (a == b) reduces to the point distance,
        # with the foot at a.
        d, fx, fy = _point_segment_distance(
            p[0], p[1], a[0], a[1], a[0], a[1])
        assert math.isclose(
            d, math.hypot(p[0] - a[0], p[1] - a[1]),
            rel_tol=1e-9, abs_tol=_ABS)
        assert (fx, fy) == (a[0], a[1])


class TestPerpDistWithinSegment:
    """Properties of _point_perp_dist_within_segment."""

    @given(p=geo_point, seg=segment)
    def test_matches_segment_distance_when_within(self, p, seg):
        # When the perpendicular foot is strictly inside the segment
        # (return is not None), the segment-clamped distance uses the
        # same foot, so the two distances agree.
        a, b = seg
        perp = _point_perp_dist_within_segment(
            p[0], p[1], a[0], a[1], b[0], b[1])
        if perp is None:
            return
        seg_d, _, _ = _point_segment_distance(
            p[0], p[1], a[0], a[1], b[0], b[1])
        assert perp >= 0.0
        assert math.isclose(perp, seg_d, rel_tol=1e-9, abs_tol=_ABS)

    @given(p=geo_point, seg=segment)
    def test_none_implies_foot_at_endpoint(self, p, seg):
        # When None (foot at/past an endpoint), the clamped foot equals
        # one of the endpoints.
        a, b = seg
        perp = _point_perp_dist_within_segment(
            p[0], p[1], a[0], a[1], b[0], b[1])
        if perp is not None:
            return
        _, fx, fy = _point_segment_distance(
            p[0], p[1], a[0], a[1], b[0], b[1])
        at_a = math.isclose(fx, a[0], abs_tol=_ABS) and math.isclose(
            fy, a[1], abs_tol=_ABS)
        at_b = math.isclose(fx, b[0], abs_tol=_ABS) and math.isclose(
            fy, b[1], abs_tol=_ABS)
        assert at_a or at_b
