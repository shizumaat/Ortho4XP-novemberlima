"""Unit tests for ``auto_patch.verification`` check exemptions.

These are pure synthetic-geometry tests (no X-Plane install, no airport
build) so they always run and pin the FALSE-POSITIVE exemptions the
build-time verification relies on — the same checks Ortho4XP runs on every
emitted patch.
"""
from __future__ import annotations

from shapely.geometry import Polygon

from auto_patch.layout import (
    PavementLayout, BuiltShape,
    ROLE_STUB, ROLE_JUNCTION, ROLE_GROUNDSIDE_PAVEMENT,
)
from auto_patch.verification import check_rect_short_edges


def _layout_with_stub(*, groundside: bool) -> PavementLayout:
    """A single stub rect spanning x=[0,50], y=[0,5].

    * end_B (right, x=50) shares its two corners with a junction polygon —
      always a legal connected terminus.
    * end_A (left, x=0) is unshared.  When ``groundside`` is True a
      groundside_pavement shape sits 1.0 m to its left (the airside↔
      groundside clearance gap) — the legitimate terminus that must NOT be
      flagged; when False, end_A genuinely ends in mid-air and MUST be
      flagged.

    Anchor is mid-tile so neither end is exempt as a tile-cut edge.
    """
    # Stub corners in (0,3)=end_A / (1,2)=end_B order.
    stub = BuiltShape(
        polygon=Polygon([(0.0, 0.0), (50.0, 0.0), (50.0, 5.0), (0.0, 5.0)]),
        role=ROLE_STUB, ref="Z")
    # Junction sharing end_B's two corners (50,0) and (50,5).
    junction = BuiltShape(
        polygon=Polygon([(50.0, 0.0), (60.0, 0.0), (60.0, 5.0), (50.0, 5.0)]),
        role=ROLE_JUNCTION)
    shapes = [stub, junction]
    if groundside:
        # Groundside ramp clipped back 1.0 m from end_A (its right edge at
        # x=-1.0, so both end_A corners at x=0 are 1.0 m away).
        shapes.append(BuiltShape(
            polygon=Polygon([(-11.0, 0.0), (-1.0, 0.0),
                             (-1.0, 5.0), (-11.0, 5.0)]),
            role=ROLE_GROUNDSIDE_PAVEMENT, ref="groundside"))
    return PavementLayout(icao="TEST", anchor=(35.22, -80.94), shapes=shapes)


def test_short_edge_genuine_disconnect_is_flagged():
    """A stub short edge with both corners unshared and nothing nearby is a
    genuine 'ends in mid-air' violation."""
    layout = _layout_with_stub(groundside=False)
    failures = check_rect_short_edges(layout)
    ends = {d.split()[0] for _i, d, _l in failures}
    assert "end_A" in ends, failures
    # end_B is shared with the junction → never flagged.
    assert "end_B" not in ends, failures


def test_short_edge_groundside_terminus_is_exempt():
    """A stub running to a groundside ramp legitimately STOPS at the airside↔
    groundside boundary (the 1.0 m clearance gap IS the connection) — it must
    NOT be flagged as ending in mid-air (KCLT taxiway J / C12 false
    positives)."""
    layout = _layout_with_stub(groundside=True)
    failures = check_rect_short_edges(layout)
    assert not failures, (
        "groundside-clearance terminus should be exempt, got: "
        + "; ".join(f"{d} @ {l}" for _i, d, l in failures))
