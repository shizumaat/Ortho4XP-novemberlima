"""Cross-strip seam-step blend: tile-seam protection (SPLP regression).

The blend reconciles metre-scale value steps between near-adjacent
vertices of different graded_strip shapes — but tile-seam-band vertices
are CROSS-TILE TERRAIN CONTRACTS (crown doctrine): each tile builds
independently with a different strip population, so a blended seam value
diverges between neighbour tiles and emits a step AT the boundary
(measured SPLP 2026-07-18: two -78-side vertices moved +2.00 m).

Hermetic: stub shapes and a stub layout whose local frame sits at a
tile meridian — no builds, no network.
"""

import math

from shapely.geometry import Polygon

from auto_patch.adjacent_ground import blend_cross_strip_seam_steps


class _StubShape:
    def __init__(self, ring, altitudes, role="graded_strip"):
        self.polygon = Polygon(ring)
        self.node_altitudes = list(altitudes) + [altitudes[0]]
        self.role = role
        self.ref = "adjacent_ground"


class _StubLayout:
    """Local metres anchored ON a tile meridian: x = 0 maps to an exact
    integer longitude, so vertices with small |x| sit in the seam band."""

    def __init__(self, shapes):
        self.shapes = shapes
        self._anchor_lat = -12.2
        self._anchor_lon = -77.0

    def m_to_ll(self, x, y):
        latitude = self._anchor_lat + y / 111320.0
        longitude = self._anchor_lon + x / (
            111320.0 * math.cos(math.radians(latitude)))
        return latitude, longitude


def _square(x0, y0, size=20.0):
    return [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size),
            (x0, y0 + size)]


def test_interior_step_blends_but_seam_band_vertices_never_move():
    # Two strips meeting at x = 200 m (far from the seam) with a 3 m
    # step: the interior seam blends.  Two more strips meeting AT the
    # meridian (x = 0, inside the 6 m seam band) with the same step:
    # protected, nothing moves.
    interior_a = _StubShape(_square(180, 100), [10.0, 10.0, 10.0, 10.0])
    interior_b = _StubShape(_square(201, 100), [13.0, 13.0, 13.0, 13.0])
    seam_a = _StubShape(_square(-24, 300), [10.0, 10.0, 10.0, 10.0])
    seam_b = _StubShape(_square(3, 300), [13.0, 13.0, 13.0, 13.0])
    layout = _StubLayout([interior_a, interior_b, seam_a, seam_b])

    moved = blend_cross_strip_seam_steps(layout.shapes, layout)

    assert moved > 0
    interior_values = (interior_a.node_altitudes[:-1]
                       + interior_b.node_altitudes[:-1])
    assert any(abs(v - 10.0) > 0.01 and abs(v - 13.0) > 0.01
               for v in interior_values), "interior step must blend"
    # seam_a's right edge (x = -4) and seam_b's left edge (x = 3) sit in
    # the 6 m seam band: their values are cross-tile contracts.
    assert seam_b.node_altitudes[:-1] == [13.0, 13.0, 13.0, 13.0]
    right_edge_values = [seam_a.node_altitudes[i]
                         for i, (x, _y) in enumerate(
                             seam_a.polygon.exterior.coords[:-1])
                         if x > -10]
    assert all(v == 10.0 for v in right_edge_values), \
        "seam-band vertices must never move"
