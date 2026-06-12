"""Unit tests for the interior-path entry measure
(docs/interior_path_entries.md §2)."""
import math

from shapely.geometry import Polygon

from auto_patch.interior_path import InteriorPathMeasure, measure_from_polys


def _notch_polygon():
    """A 100x30 slab with a 20 m-deep grass notch cut into the top edge
    between x=40..60 — the CYXY n72/n88 shape: the straight chord
    between the two top lobes crosses the notch; the interior path goes
    around its bottom."""
    slab = Polygon([(0, 0), (100, 0), (100, 30), (0, 30)])
    notch = Polygon([(40, 10), (60, 10), (60, 31), (40, 31)])
    return slab.difference(notch)


def test_straight_inside_is_straight():
    m = InteriorPathMeasure(_notch_polygon())
    d = m.distance((10, 5), (90, 5))
    assert d is not None
    assert abs(d - 80.0) < 0.1


def test_notch_pair_goes_the_long_way():
    m = InteriorPathMeasure(_notch_polygon())
    straight = math.hypot(70 - 30, 0.0)
    d = m.distance((30, 25), (70, 25))
    assert d is not None
    # interior path must dip under the notch (y<=10): strictly longer
    # than the chord, and roughly the dip-around length
    assert d > straight + 5.0
    assert d < straight + 40.0


def test_islands_have_no_coupling():
    a = Polygon([(0, 0), (30, 0), (30, 30), (0, 30)])
    b = Polygon([(100, 0), (130, 0), (130, 30), (100, 30)])
    m = measure_from_polys([a, b])
    assert m.distance((15, 15), (115, 15)) is None


def test_deterministic():
    m1 = InteriorPathMeasure(_notch_polygon())
    m2 = InteriorPathMeasure(_notch_polygon())
    assert m1.distance((30, 25), (70, 25)) == m2.distance((30, 25),
                                                          (70, 25))


def test_zero_gap():
    m = InteriorPathMeasure(_notch_polygon())
    assert m.distance((10, 10), (10, 10)) == 0.0
