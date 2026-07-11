"""Regression tests for the tunnel graze-clip node_altitudes resample.

Part-36 queue item 9: the tunnel graze-clip path (``bridges.py``
``_finalize_tunnel_emission``) clips a portal piece off a grazed
pavement edge with ``piece.difference(pavement.buffer(0.6))`` and then
rebuilds the piece's ``node_altitudes`` for the clipped ring via
``_resample_node_altitudes_nn``.

The buffered-gate cut introduces new vertices in the piece INTERIOR
(the offset arc / corner-poke of the pavement).  The plain
nearest-neighbour pass-2 fallback snaps such an interior vertex to the
nearest old ring *vertex* altitude, which on a sloped, sparse-ring
piece is wrong by ``gradient x (distance to the nearest corner)`` —
metre-scale.  The ``interior_edge_project=True`` fallback projects onto
the nearest old *edge* and interpolates, recovering the boundary
gradient.

These tests drive that exact clip at unit scale (no full build):
  * document the plain-NN hazard (metre-scale error on a sloped piece);
  * assert the edge-projection fallback removes it;
  * assert the fallback is a no-op when there is no interior vertex
    (straight-edge graze, the SPJC mouth shape) and for a dense
    DEM-following ring (real tunnel wall-band geometry);
  * assert the default (``interior_edge_project=False``) still returns
    the plain-NN result, so no existing caller changes.
"""
import os
import sys

import pytest
from shapely.geometry import Polygon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "src"), ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from auto_patch.elevation import _resample_node_altitudes_nn  # noqa: E402
from auto_patch.bridges import _TUNNEL_GRAZE_CLEARANCE_M       # noqa: E402


# The _rect_from_axis convention: corners c0,c3 = high edge; c1,c2 =
# low edge.  Here the gradient runs along +x: x=0 is high, x=40 is low.
_CORNERS = [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)]


def _closed_alts(corner_alts):
    return [round(a, 1) for a in corner_alts] + [round(corner_alts[0], 1)]


def _true_planar_alt(x, corner_alts):
    """True planar altitude at abscissa ``x`` on the x-gradient ramp."""
    alt_high = (corner_alts[0] + corner_alts[3]) / 2.0   # x = 0
    alt_low = (corner_alts[1] + corner_alts[2]) / 2.0    # x = 40
    t = max(0.0, min(1.0, x / 40.0))
    return alt_high + t * (alt_low - alt_high)


def _graze_clip(piece, gate):
    cut = piece.difference(gate.buffer(_TUNNEL_GRAZE_CLEARANCE_M))
    if cut.geom_type == "MultiPolygon":
        cut = max(cut.geoms, key=lambda g: g.area)
    return cut


def _worst_error(resampled, new_ring, corner_alts):
    return max(abs(resampled[i] - _true_planar_alt(x, corner_alts))
               for i, (x, _y) in enumerate(new_ring))


# A pavement corner poking up into the middle of the piece's bottom
# long edge — produces an interior cut vertex ~20 m from either corner.
_CORNER_POKE_GATE = Polygon([(15.0, -30.0), (25.0, -30.0), (20.0, 0.0)])


@pytest.mark.parametrize("corner_alts,expected_nn_error", [
    ([100.0, 98.4, 98.4, 100.0], 0.5),   # 4 % ramp -> >= 0.5 m NN error
    ([100.0, 92.0, 92.0, 100.0], 2.0),   # 20 % ramp -> >= 2 m NN error
])
def test_corner_poke_graze_edge_projection_beats_nn(
        corner_alts, expected_nn_error):
    piece = Polygon(_CORNERS)
    oring = list(piece.exterior.coords)[:-1]
    na = _closed_alts(corner_alts)

    cut = _graze_clip(piece, _CORNER_POKE_GATE)
    new_ring = list(cut.exterior.coords)
    # The corner poke must have introduced interior cut vertices.
    assert len(new_ring) > len(oring) + 1

    nn = _resample_node_altitudes_nn(cut, oring, list(na))
    fixed = _resample_node_altitudes_nn(
        cut, oring, list(na), interior_edge_project=True)

    nn_err = _worst_error(nn, new_ring, corner_alts)
    fixed_err = _worst_error(fixed, new_ring, corner_alts)

    # The hazard: plain vertex-NN is metre-scale wrong here.
    assert nn_err >= expected_nn_error, (nn_err, expected_nn_error)
    # The fix: edge projection is within rounding of the true gradient.
    assert fixed_err <= 0.1, (fixed_err, nn_err)
    # And it is a strict, large improvement.
    assert fixed_err < nn_err - 0.4


def test_straight_edge_graze_has_no_interior_vertex():
    """SPJC mouth shape: a straight runway edge grazes the piece.  No
    interior cut vertex is created, so NN is never invoked and both
    modes agree (edge projection is a no-op)."""
    corner_alts = [100.0, 98.4, 98.4, 100.0]
    piece = Polygon(_CORNERS)
    oring = list(piece.exterior.coords)[:-1]
    na = _closed_alts(corner_alts)

    straight_gate = Polygon([(10.0, -30.0), (30.0, -30.0),
                             (30.0, -0.6), (10.0, -0.6)])
    cut = _graze_clip(piece, straight_gate)

    nn = _resample_node_altitudes_nn(cut, oring, list(na))
    fixed = _resample_node_altitudes_nn(
        cut, oring, list(na), interior_edge_project=True)
    assert nn == fixed
    # Every new vertex sits on an old edge -> exact interpolation.
    new_ring = list(cut.exterior.coords)
    assert _worst_error(fixed, new_ring, corner_alts) <= 0.1


def test_dense_dem_following_ring_small_error_either_way():
    """Real tunnel wall-band geometry is a dense DEM-following ring, so
    even plain NN has sub-decimetre error (the nearest ring vertex is
    close).  The edge-projection fallback must not make it worse."""
    ring, alts = [], []
    for i in range(21):                      # top edge, 2 m spacing
        x = i * 2.0
        ring.append((x, 1.0))
        alts.append(round(100.0 + 0.04 * x, 1))
    for i in range(21):                      # bottom edge back
        x = (20 - i) * 2.0
        ring.append((x, 0.0))
        alts.append(round(100.0 + 0.04 * x, 1))
    alts_closed = alts + [alts[0]]
    piece = Polygon(ring)
    oring = list(piece.exterior.coords)[:-1]

    gate = Polygon([(18.0, -30.0), (22.0, -30.0), (20.0, 0.6)])
    cut = _graze_clip(piece, gate)
    new_ring = list(cut.exterior.coords)

    def worst(res):
        return max(abs(res[i] - (100.0 + 0.04 * x))
                   for i, (x, _y) in enumerate(new_ring))

    nn = _resample_node_altitudes_nn(cut, oring, list(alts_closed))
    fixed = _resample_node_altitudes_nn(
        cut, oring, list(alts_closed), interior_edge_project=True)
    assert worst(nn) <= 0.2
    assert worst(fixed) <= 0.2


def test_default_mode_is_plain_nn():
    """The default (no keyword) must be byte-identical to the plain-NN
    path, so no existing caller changes behaviour."""
    corner_alts = [100.0, 92.0, 92.0, 100.0]
    piece = Polygon(_CORNERS)
    oring = list(piece.exterior.coords)[:-1]
    na = _closed_alts(corner_alts)
    cut = _graze_clip(piece, _CORNER_POKE_GATE)

    default = _resample_node_altitudes_nn(cut, oring, list(na))
    explicit_off = _resample_node_altitudes_nn(
        cut, oring, list(na), interior_edge_project=False)
    assert default == explicit_off
    # And it must differ from the edge-projection result (proving the
    # flag actually gates behaviour).
    on = _resample_node_altitudes_nn(
        cut, oring, list(na), interior_edge_project=True)
    assert default != on
