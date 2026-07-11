"""Slice B stage B3 order 3 — taxiway-end WRAP + tunnel-ramp STANDOFF.

Two independently gated adjacent-ground geometry features
(``adjacent_ground.py``):

  * SCOPE A — taxiway-end wrap (``O4_ADJACENT_GROUND_END_WRAP``): the
    band corridor continues around a taxiway END that abuts a runway-END
    skirt, at the family clearance distance, instead of stopping where the
    terrain-facing probe hits the skirt.  The skirt is the JOIN target, not
    an obstruction.
  * SCOPE B — tunnel-ramp standoff (``O4_ADJACENT_GROUND_TUNNEL_STANDOFF``):
    band construction excludes a 1 m standoff around tunnel mouth pieces
    (``tunnel_ramp`` / ``retaining_wall``) so a strip never welds onto the
    steep mouth-ramp floor — the building/groundside standoff pattern.
    Object-bridge plates (``bridge_trench`` / ``bridge_causeway``) are NOT
    stood off.

Both default OFF; the gate-off no-op is asserted explicitly.
"""
import types

from shapely.geometry import Polygon
from shapely.prepared import prep

from auto_patch.config import (
    CLEARANCE_MAX_REACH_M,
    CLEARANCE_STATION_STEP_M,
    taxiway_strip_graded_half_width_for_letter,
)
from auto_patch.grade_law import adjacent_ground_envelope
from auto_patch import adjacent_ground as AG

STEP = CLEARANCE_STATION_STEP_M
EDGE_ALT = 100.0
TRIGGER = 1.0


def _shape(role, polygon, ref=None):
    return types.SimpleNamespace(role=role, ref=ref, polygon=polygon)


def _layout(shapes):
    return types.SimpleNamespace(shapes=shapes)


def _taxi_c_fns():
    def ceil_off(d):
        return adjacent_ground_envelope("taxiway", None, "C", d)[1]

    def envelope_at(d):
        return adjacent_ground_envelope("taxiway", None, "C", d)

    def floor_depth(d):
        f = adjacent_ground_envelope("taxiway", None, "C", min(d, width))[0]
        return None if f is None else -f
    width = taxiway_strip_graded_half_width_for_letter("C")
    reach = CLEARANCE_MAX_REACH_M["taxiway"]
    return ceil_off, envelope_at, floor_depth, width, reach


# ── A rectangular taxiway whose EAST end (x=100) abuts a "skirt" region ──
def _taxi_rect():
    # CCW rectangle 100 m long, 20 m wide; outward normal of the east edge
    # (x=100, y:0->20 going up on a CCW ring) points +x, toward the skirt.
    return [(0.0, 0.0), (100.0, 0.0), (100.0, 20.0), (0.0, 20.0), (0.0, 0.0)]


def _skirt_beyond_east_end():
    # Covers the east-edge probe point (100 + 1.5 = 101.5, y).
    return Polygon([(101.0, -5.0), (130.0, -5.0), (130.0, 25.0),
                    (101.0, 25.0)])


def _rising_dem():
    # DEM 5 m above the edge everywhere → cut fires at every non-skipped
    # station.
    def dem(x, y):
        return EDGE_ALT + 5.0
    return dem


def _n_refs(st_alts):
    """Number of stations that kept a terrain-facing reference (were NOT
    skipped by the probe / end-edge test)."""
    return sum(1 for a in st_alts if a is not None)


def _east_coverage(bands):
    """Max x reached by any emitted band ring — how far the corridor wraps
    toward / past the east end (x=100)."""
    xs = [x for ring, _ in bands for x, _y in ring]
    return max(xs) if xs else float("-inf")


class TestTaxiwayEndWrap:
    def _derive(self, wrap_prep):
        ceil_off, envelope_at, floor_depth, width, reach = _taxi_c_fns()
        coords = _taxi_rect()
        prep_static = prep(_skirt_beyond_east_end())
        return AG._derive_shape_stations_and_bands(
            coords, True, [EDGE_ALT] * len(coords), None, width, reach,
            TRIGGER, floor_depth, ceil_off, STEP, prep_static, set(),
            _rising_dem(), wrap_skirt_prep=wrap_prep)

    def test_wrap_keeps_more_stations_at_the_skirt_end(self):
        _f0, _c0, _s0, st_alts0, _o0 = self._derive(None)
        _f1, _c1, _s1, st_alts1, _o1 = self._derive(
            prep(_skirt_beyond_east_end()))
        # With the skirt declared the join target, the skirt-facing end
        # stations keep their reference (they were skipped before).
        assert _n_refs(st_alts1) > _n_refs(st_alts0)

    def test_wrap_extends_coverage_toward_the_skirt(self):
        f0, c0, _s0, _a0, _o0 = self._derive(None)
        f1, c1, _s1, _a1, _o1 = self._derive(
            prep(_skirt_beyond_east_end()))
        # The corridor now reaches the east end (x≈100) / wraps past it,
        # whereas without the wrap it stops short.
        assert _east_coverage(f1 + c1) > _east_coverage(f0 + c0)

    def test_non_skirt_obstruction_still_skips_under_wrap(self):
        # wrap_skirt_prep that does NOT contain the east probe → the static
        # hit is a real obstruction and still skips (only skirts join): the
        # reference count is unchanged from the gate-off march.
        ceil_off, _env, floor_depth, width, reach = _taxi_c_fns()
        coords = _taxi_rect()
        obstruction = _skirt_beyond_east_end()          # the static block
        elsewhere = Polygon([(-50, -50), (-40, -50), (-40, -40),
                             (-50, -40)])                # skirt far away
        base = AG._derive_shape_stations_and_bands(
            coords, True, [EDGE_ALT] * len(coords), None, width, reach,
            TRIGGER, floor_depth, ceil_off, STEP, prep(obstruction),
            set(), _rising_dem())
        wrapped_elsewhere = AG._derive_shape_stations_and_bands(
            coords, True, [EDGE_ALT] * len(coords), None, width, reach,
            TRIGGER, floor_depth, ceil_off, STEP, prep(obstruction),
            set(), _rising_dem(), wrap_skirt_prep=prep(elsewhere))
        assert _n_refs(wrapped_elsewhere[3]) == _n_refs(base[3])


class TestTunnelRampStandoff:
    def test_block_none_without_tunnel_shapes(self):
        lay = _layout([_shape("junction", Polygon(
            [(0, 0), (10, 0), (10, 10), (0, 10)]))])
        assert AG._tunnel_ramp_standoff_block(lay) is None

    def test_block_buffers_tunnel_ramp_and_wall(self):
        ramp = Polygon([(0, 0), (10, 0), (10, 4), (0, 4)])
        wall = Polygon([(20, 0), (24, 0), (24, 4), (20, 4)])
        lay = _layout([_shape("tunnel_ramp", ramp),
                       _shape("retaining_wall", wall)])
        block = AG._tunnel_ramp_standoff_block(lay)
        assert block is not None and not block.is_empty
        # 1 m buffer: a point 0.5 m outside the ramp edge is inside the block.
        assert block.contains(Polygon(
            [(10.4, 1), (10.6, 1), (10.6, 3), (10.4, 3)]).centroid)
        # 2 m outside is not.
        assert not block.contains(
            Polygon([(12.5, 1), (13, 1), (13, 3), (12.5, 3)]).centroid)

    def test_bridge_plates_are_not_stood_off(self):
        # Object-bridge plates must NOT enter the standoff block (they are
        # pavement-equivalent hard graph members).
        trench = Polygon([(0, 0), (10, 0), (10, 4), (0, 4)])
        cause = Polygon([(20, 0), (24, 0), (24, 4), (20, 4)])
        lay = _layout([_shape("bridge_trench", trench),
                       _shape("bridge_causeway", cause)])
        assert AG._tunnel_ramp_standoff_block(lay) is None


class TestGatesDefaultOffNoOp:
    def test_module_gates_default_off(self):
        assert AG._END_WRAP is False
        assert AG._TUNNEL_STANDOFF is False

    def test_wrap_none_equals_gate_off_march(self):
        # wrap_skirt_prep=None (the gate-off path) reproduces the pre-wrap
        # march byte-for-byte: stations, references, and bands identical to
        # a call that omits the parameter entirely.
        ceil_off, envelope_at, floor_depth, width, reach = _taxi_c_fns()
        coords = _taxi_rect()
        prep_static = prep(_skirt_beyond_east_end())
        args = (coords, True, [EDGE_ALT] * len(coords), None, width, reach,
                TRIGGER, floor_depth, ceil_off, STEP, prep_static, set(),
                _rising_dem())
        a = AG._derive_shape_stations_and_bands(*args)
        b = AG._derive_shape_stations_and_bands(*args, wrap_skirt_prep=None)
        assert a[2] == b[2]         # stations
        assert a[3] == b[3]         # st_alts
        assert [r for r, _ in a[0]] == [r for r, _ in b[0]]   # fill rings
        assert [r for r, _ in a[1]] == [r for r, _ in b[1]]   # cut rings
