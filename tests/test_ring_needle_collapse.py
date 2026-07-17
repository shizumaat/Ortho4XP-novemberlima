"""Defect 4a — degenerate ring-needle collapse (pipeline pre-solve cleanup).

A ring needle is an apex whose interior angle is below
``_NEEDLE_ANGLE_DEG`` while BOTH bounding edges exceed ``_NEEDLE_MIN_EDGE_M``
— a construction artifact (the slice/weld chain folding a long thin tongue
to a near-collinear spike, e.g. KBNA junction 289's 2.8 deg apex between
48.6 m and 57.6 m edges).  ``_collapse_ring_needles`` drops the apex; the
pre-solve ``_dedup_coincident_ring_vertices(collapse_needles=True)`` pass
applies it to airside shapes only.
"""
import math
import types

from shapely.geometry import Polygon

from auto_patch.pipeline import (
    _NEEDLE_ANGLE_DEG,
    _NEEDLE_MAX_DROP_AREA_M2,
    _NEEDLE_MIN_EDGE_M,
    _collapse_ring_needles,
    _dedup_coincident_ring_vertices,
)


def _min_interior_angle(coords_open):
    n = len(coords_open)
    worst = 999.0
    for i in range(n):
        a = coords_open[(i - 1) % n]
        b = coords_open[i]
        c = coords_open[(i + 1) % n]
        v1 = (a[0] - b[0], a[1] - b[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        n1 = math.hypot(*v1)
        n2 = math.hypot(*v2)
        if n1 < 1e-9 or n2 < 1e-9:
            continue
        cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
        worst = min(worst, math.degrees(math.acos(cos)))
    return worst


def _spike_ring():
    """A rectangle with a long thin outward tongue ending in a ~6 deg apex
    (edges ~40 m, well over the 8 m guard).  The apex triangle is 80 m² —
    below the ``_NEEDLE_MAX_DROP_AREA_M2`` real-pavement cap, so it is a
    genuine zero-width artifact that collapses."""
    return [
        (0.0, 0.0), (100.0, 0.0), (100.0, 40.0),
        (52.0, 40.0),            # tongue base near side
        (50.0, 80.0),            # APEX — 40 m out, ~6 deg between the sides
        (48.0, 40.0),            # tongue base far side
        (0.0, 40.0),
    ]


class TestCollapseRingNeedles:
    def test_drops_wide_edged_spike_apex(self):
        ring = _spike_ring()
        assert _min_interior_angle(ring) < _NEEDLE_ANGLE_DEG
        out, na, dropped = _collapse_ring_needles(ring, None)
        assert dropped == 1
        assert len(out) == len(ring) - 1
        assert _min_interior_angle(out) >= _NEEDLE_ANGLE_DEG

    def test_keeps_short_edged_sharp_corner(self):
        # A sharp corner whose bounding edges are SHORT (< 8 m) is a genuine
        # taper toe, not a construction spike — the edge guard protects it.
        ring = [(0.0, 0.0), (100.0, 0.0), (100.0, 40.0),
                (5.0, 40.0), (4.5, 46.0), (4.0, 40.0), (0.0, 40.0)]
        # apex at (4.5,46): edges to (5,40) and (4,40) are ~6 m — below guard.
        _out, _na, dropped = _collapse_ring_needles(ring, None)
        assert dropped == 0

    def test_keeps_real_pavement_tip_over_area_cap(self):
        # A sub-10-deg apex on LONG edges but enclosing MORE than the
        # real-pavement cap is a genuine (thin) pavement wedge tip — deleting
        # it would carve out live pavement, so the area guard keeps it.
        # apex (50,220): edges to (55,40)/(45,40) are ~180 m; triangle area
        # = 0.5 * base(10) * height(180) = 900 m² > 100 m² cap.
        ring = [(0.0, 0.0), (100.0, 0.0), (100.0, 40.0),
                (55.0, 40.0), (50.0, 220.0), (45.0, 40.0), (0.0, 40.0)]
        angle = _min_interior_angle(ring)
        assert angle < _NEEDLE_ANGLE_DEG        # it IS a needle by angle
        _out, _na, dropped = _collapse_ring_needles(ring, None)
        assert dropped == 0                     # ...but kept: > area cap

    def test_altitudes_stay_aligned(self):
        ring = _spike_ring()
        na = [float(i) for i in range(len(ring))]
        out, out_na, dropped = _collapse_ring_needles(ring, list(na))
        assert dropped == 1
        assert out_na is not None
        assert len(out_na) == len(out)
        # The apex altitude (index 4) was removed; the rest kept order.
        assert 4.0 not in out_na

    def test_no_collapse_leaves_ring_and_na_untouched(self):
        ring = [(0.0, 0.0), (100.0, 0.0), (100.0, 40.0), (0.0, 40.0)]
        na = [10.0, 11.0, 12.0, 13.0]
        out, out_na, dropped = _collapse_ring_needles(ring, list(na))
        assert dropped == 0
        assert out == ring
        assert out_na == na


def _shape(role, ring, na=None):
    poly = Polygon(ring + [ring[0]])
    return types.SimpleNamespace(role=role, polygon=poly, node_altitudes=na)


class TestDedupPassNeedleCollapse:
    def test_airside_shape_needle_collapsed_when_enabled(self):
        s = _shape("junction", _spike_ring())
        layout = types.SimpleNamespace(shapes=[s])
        _dedup_coincident_ring_vertices(layout, "TEST",
                                        collapse_needles=True)
        open_coords = list(s.polygon.exterior.coords)[:-1]
        assert _min_interior_angle(open_coords) >= _NEEDLE_ANGLE_DEG

    def test_needle_kept_when_flag_off(self):
        s = _shape("junction", _spike_ring())
        layout = types.SimpleNamespace(shapes=[s])
        _dedup_coincident_ring_vertices(layout, "TEST")
        open_coords = list(s.polygon.exterior.coords)[:-1]
        assert _min_interior_angle(open_coords) < _NEEDLE_ANGLE_DEG

    def test_non_airside_shape_left_alone(self):
        # A tunnel_ramp (object story, not airside pavement) keeps its needle
        # even with the flag on — the collapse is airside-only.
        s = _shape("tunnel_ramp", _spike_ring())
        layout = types.SimpleNamespace(shapes=[s])
        _dedup_coincident_ring_vertices(layout, "TEST",
                                        collapse_needles=True)
        open_coords = list(s.polygon.exterior.coords)[:-1]
        assert _min_interior_angle(open_coords) < _NEEDLE_ANGLE_DEG

    def test_threshold_values(self):
        # Widened 2026-07-16 (KBNA Donelson round 8) from 5.0/10.0 so the
        # surviving 7.5-9.7 deg spikes on junctions 289/290 collapse, with an
        # area cap so no real-pavement tip is ever carved out.
        assert _NEEDLE_ANGLE_DEG == 10.0
        assert _NEEDLE_MIN_EDGE_M == 8.0
        assert _NEEDLE_MAX_DROP_AREA_M2 == 100.0
