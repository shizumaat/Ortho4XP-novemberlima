"""Smoke tests for auto_patch.elevation_smoothing.

These helpers are dormant behind ``USE_PER_POLYGON_ELEVATION_FIELD``
in the main pipeline, so they aren't exercised by the integration
suite.  This module ensures they're at least loadable and callable
without NameError — the failure mode that the missing ``Point`` /
``Optional`` imports would have produced the moment the flag flips on.
"""
from shapely.geometry import Polygon

# elevation_smoothing and elevation have a (pre-existing) circular
# import; in normal use elevation loads first.  Import it first here
# so elevation_smoothing resolves, then pull the helper under test.
import auto_patch.elevation  # noqa: F401
from auto_patch.elevation_smoothing import _smooth_polygon_grid


def test_smooth_polygon_grid_runs_without_nameerror():
    """A trivial square + one hard anchor must build the inside mask
    (which calls Point(...)) and return a sampler or None — never
    NameError on an unimported symbol."""
    poly = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    anchors = [(50.0, 50.0, 1000.0)]
    result = _smooth_polygon_grid(
        poly, anchors,
        dem=None, tile_lat=40, tile_lon=-100,
        layout_anchor=(40.0, -100.0))
    # Either a callable sampler or None — both are valid outcomes.
    assert result is None or callable(result) or isinstance(result, tuple)


def test_smooth_polygon_grid_degenerate_returns_none():
    """Empty / zero-span polygons return None early (before the
    Point-using inside-mask loop)."""
    assert _smooth_polygon_grid(
        Polygon(), [], dem=None, tile_lat=0, tile_lon=0,
        layout_anchor=(0.0, 0.0)) is None
