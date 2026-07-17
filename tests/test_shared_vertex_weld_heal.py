"""Shared-vertex weld heal-not-drop (KBNA Donelson 2026-07-16).

``_enforce_shared_vertices`` collapses near-coincident vertices to a canonical
point.  On a THIN airside wedge that can pull the shape's own opposite-edge
vertices together, making the rewritten ring invalid.  The pre-fix code then

  * emptied the shape when it collapsed to < 3 distinct vertices, and
  * kept only the LARGEST buffer(0) lobe when the ring pinched/folded,

both of which silently DELETED real pavement (a 4,928 m² taxiway hole at
KBNA).  The heal keeps the pavement: a < 3-vertex collapse reverts to the
original polygon, a clean pinch re-admits every lobe, and a genuine fold
reverts to the original — the total airside footprint is preserved and the
build-time drop counter (``layout.airside_weld_drops``) stays empty.
"""
import math

from shapely.geometry import Polygon

from auto_patch.layout import BuiltShape, PavementLayout, ROLE_JUNCTION
from auto_patch.pavement.vertices import (
    _enforce_shared_vertices,
    _record_airside_drop,
)


def _airside_area(layout):
    return sum(s.polygon.area for s in layout.shapes
               if s.polygon is not None and not s.polygon.is_empty)


class TestWeldHealNotDrop:
    def test_thin_wedge_collapse_is_kept_not_emptied(self):
        # A thin triangle whose two left corners are 1 m apart: the 1.5 m weld
        # pulls them together, collapsing the rewritten ring to < 3 vertices.
        # The heal keeps the ORIGINAL polygon rather than emptying it.
        poly = Polygon([(0.0, 0.0), (50.0, 0.0), (0.0, 1.0)])
        area_before = poly.area
        s = BuiltShape(polygon=poly, role=ROLE_JUNCTION)
        layout = PavementLayout(icao="TEST", anchor=(0.0, 0.0), shapes=[s])
        _enforce_shared_vertices(layout, tol=1.5)
        surviving = [x for x in layout.shapes
                     if x.polygon is not None and not x.polygon.is_empty]
        assert surviving, "thin wedge was silently emptied (pavement drop)"
        assert _airside_area(layout) >= area_before - 1.0
        # No > 100 m² drop was recorded (this one is small anyway).
        assert not getattr(layout, "airside_weld_drops", [])

    def test_pinched_ring_keeps_all_lobes(self):
        # An hourglass whose waist vertices (index 2 and index 5, NON-adjacent)
        # sit 0.4 m apart: the weld merges them to one canonical point, pinching
        # the ring into two lobes that TOUCH at the waist.  Every lobe must
        # survive — the total footprint is preserved, not halved.
        ring = [(0.0, 0.0), (10.0, 0.0), (5.0, 4.8),
                (10.0, 10.0), (0.0, 10.0), (5.0, 5.2)]
        src = Polygon(ring)
        assert src.is_valid, "test premise: hourglass must be a simple polygon"
        area_before = src.area
        s = BuiltShape(polygon=src, role=ROLE_JUNCTION)
        layout = PavementLayout(icao="TEST", anchor=(0.0, 0.0), shapes=[s])
        _enforce_shared_vertices(layout, tol=1.0)
        # Footprint preserved (no lobe discarded) within rounding.
        assert _airside_area(layout) >= area_before - 2.0
        # Every surviving piece is a valid non-empty airside polygon.
        for x in layout.shapes:
            if x.polygon is not None and not x.polygon.is_empty:
                assert x.role == ROLE_JUNCTION
                assert x.polygon.geom_type == "Polygon"
        assert not getattr(layout, "airside_weld_drops", [])

    def test_normal_shapes_unaffected(self):
        # A well-formed junction far from any other vertex is untouched.
        poly = Polygon([(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)])
        s = BuiltShape(polygon=poly, role=ROLE_JUNCTION)
        layout = PavementLayout(icao="TEST", anchor=(0.0, 0.0), shapes=[s])
        _enforce_shared_vertices(layout, tol=1.5)
        assert math.isclose(s.polygon.area, 1200.0, rel_tol=1e-6)


class TestAirsideDropCounter:
    def test_records_and_logs_large_airside_drop(self):
        poly = Polygon([(0.0, 0.0), (100.0, 0.0), (100.0, 100.0),
                        (0.0, 100.0)])
        s = BuiltShape(polygon=poly, role=ROLE_JUNCTION)
        layout = PavementLayout(icao="TEST", anchor=(36.0, -86.0), shapes=[s])
        _record_airside_drop(layout, s, poly, 4928.0, "unit-test")
        events = getattr(layout, "airside_weld_drops", [])
        assert len(events) == 1
        assert events[0]["role"] == ROLE_JUNCTION
        assert events[0]["area_m2"] == 4928.0
        assert events[0]["mechanism"] == "unit-test"

    def test_small_drop_below_floor_not_recorded(self):
        poly = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (0.0, 5.0)])
        s = BuiltShape(polygon=poly, role=ROLE_JUNCTION)
        layout = PavementLayout(icao="TEST", anchor=(36.0, -86.0), shapes=[s])
        _record_airside_drop(layout, s, poly, 50.0, "unit-test")
        assert not getattr(layout, "airside_weld_drops", [])

    def test_non_airside_drop_not_recorded(self):
        poly = Polygon([(0.0, 0.0), (100.0, 0.0), (100.0, 100.0),
                        (0.0, 100.0)])
        s = BuiltShape(polygon=poly, role="graded_strip")
        layout = PavementLayout(icao="TEST", anchor=(36.0, -86.0), shapes=[s])
        _record_airside_drop(layout, s, poly, 4928.0, "unit-test")
        assert not getattr(layout, "airside_weld_drops", [])
