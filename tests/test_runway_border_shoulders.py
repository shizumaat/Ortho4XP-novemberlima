"""Unit tests for border-strip-derived runway shoulder detection.

The detector reads wide draped ``.lin`` border strips traced along a
runway's outline (KBNA construction style) and derives THE runway's
shoulder width from the strips' declared width (``width / 2``,
arc-length-weighted median across both edges).  The pipeline injects
the result into the apt.dat coded-shoulder path, which widens
symmetrically.  Headless, synthetic geometry only.
"""
from types import SimpleNamespace

import pytest
from shapely.geometry import LineString

from auto_patch.pavement.runways import (
    _detect_runway_border_strip_shoulders,
)


def _identity_to_m(lon, lat):
    """Treat (lon, lat) as meter coordinates directly."""
    return (lon, lat)


def _runway(length_m=1000.0, width_m=46.0):
    return SimpleNamespace(
        lon_a=0.0, lat_a=0.0, lon_b=length_m, lat_b=0.0,
        width_m=width_m)


_KW = dict(
    edge_tol_m=3.0,
    sample_step_m=5.0,
    min_strip_cover_m=40.0,
    min_side_cover_m=300.0,
    min_w=2.0,
    max_w=15.0,
)


def test_left_edge_strip_declares_runway_shoulder():
    r = _runway()          # half = 23.0
    strips = [(LineString([(50.0, -23.0), (950.0, -23.0)]), 24.0)]
    assert _detect_runway_border_strip_shoulders(
        r, _identity_to_m, strips, **_KW) == pytest.approx(12.0)


def test_two_sides_pool_into_one_weighted_median_width():
    # Left carries 900 m of 24 m strip, right 900 m of 20 m strip —
    # the pooled arc-length-weighted median of {12.0 x900, 10.0 x900}
    # lands on the lower half-width at the 50 % accumulation point.
    r = _runway()
    strips = [
        (LineString([(50.0, -23.0), (950.0, -23.0)]), 24.0),
        (LineString([(50.0, 23.0), (950.0, 23.0)]), 20.0),
    ]
    assert _detect_runway_border_strip_shoulders(
        r, _identity_to_m, strips, **_KW) == pytest.approx(10.0)


def test_crossing_strip_is_not_shoulder_evidence():
    # A taxiway border CROSSING the runway puts only ~6 m of arc near
    # each edge — below the per-strip floor.
    r = _runway()
    strips = [(LineString([(500.0, -200.0), (500.0, 200.0)]), 24.0)]
    assert _detect_runway_border_strip_shoulders(
        r, _identity_to_m, strips, **_KW) is None


def test_short_side_coverage_stays_unqualified():
    # 160 m of on-edge arc is real but below the 300 m side floor
    # (KBNA 13/31's right side alone would not qualify the runway).
    r = _runway()
    strips = [(LineString([(400.0, 23.0), (560.0, 23.0)]), 24.0)]
    assert _detect_runway_border_strip_shoulders(
        r, _identity_to_m, strips, **_KW) is None


def test_one_qualifying_side_admits_other_side_contributions():
    # Left qualifies on its own (>=300 m); the right's shorter 160 m
    # fragment then joins the pooled median instead of being dropped.
    r = _runway()
    strips = [
        (LineString([(50.0, -23.0), (950.0, -23.0)]), 24.0),
        (LineString([(400.0, 23.0), (560.0, 23.0)]), 24.0),
    ]
    assert _detect_runway_border_strip_shoulders(
        r, _identity_to_m, strips, **_KW) == pytest.approx(12.0)


def test_width_clamped_to_max():
    r = _runway()
    strips = [(LineString([(50.0, -23.0), (950.0, -23.0)]), 40.0)]
    assert _detect_runway_border_strip_shoulders(
        r, _identity_to_m, strips, **_KW) == pytest.approx(15.0)


def test_weighted_median_prefers_dominant_strip_width():
    # 24 m strips dominate the left edge; one 31 m strip contributes a
    # shorter run (KBNA 13/31: junction border overlapping the edge).
    r = _runway(length_m=2000.0)
    strips = [
        (LineString([(0.0, -23.0), (800.0, -23.0)]), 24.0),
        (LineString([(850.0, -23.0), (1650.0, -23.0)]), 24.0),
        (LineString([(1700.0, -23.0), (1900.0, -23.0)]), 31.0),
    ]
    assert _detect_runway_border_strip_shoulders(
        r, _identity_to_m, strips, **_KW) == pytest.approx(12.0)


def test_off_extent_samples_ignored():
    # A strip on the edge line but wholly beyond the runway end
    # contributes nothing.
    r = _runway()
    strips = [(LineString([(1100.0, -23.0), (1600.0, -23.0)]), 24.0)]
    assert _detect_runway_border_strip_shoulders(
        r, _identity_to_m, strips, **_KW) is None
