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
    ROLE_STUB, ROLE_JUNCTION, ROLE_GROUNDSIDE_PAVEMENT, ROLE_RUNWAY,
)
from auto_patch.verification import check_rect_short_edges, check_runway_profile


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


def _single_poly_runway_with_interior_bump() -> BuiltShape:
    """A DE-SEGMENTED single-poly runway ring (``from_single_poly``) 1000 m
    long × 30 m wide whose profile is FLAT at both physical ends but bulges
    +10 m at the mid station (x=500).

    The ring's vertices, going bottom edge then top edge, encode three
    profile stations (x = 0, 500, 1000) as interior long-edge vertices:

      * end→mid grade = 10 m / 500 m = 2.0 % — a clear > 1.5 % violation;
      * end→end grade (what the OLD extreme-station cross-end sampler saw,
        clustering only the two physical ends) = 0 % — INVISIBLE.

    This is exactly the de-seg blind spot: the whole interior profile went
    dark once runways emitted as one ring per ref instead of a chain of
    sub-rects.
    """
    coords = [
        (0.0, -15.0), (500.0, -15.0), (1000.0, -15.0),   # bottom edge
        (1000.0, 15.0), (500.0, 15.0), (0.0, 15.0),      # top edge
    ]
    node_altitudes = [0.0, 10.0, 0.0, 0.0, 10.0, 0.0]
    return BuiltShape(
        polygon=Polygon(coords), role=ROLE_RUNWAY, ref="02/20",
        node_altitudes=node_altitudes, from_single_poly=True)


def test_single_poly_runway_interior_grade_is_flagged():
    """The de-seg fix: a single-poly runway ring's interior profile stations
    are sampled per-station, so a > 1.5 % grade hidden between the physical
    ends (invisible to the extreme-station cross-end sampler) is caught."""
    shape = _single_poly_runway_with_interior_bump()
    layout = PavementLayout(icao="TEST", anchor=(35.22, -80.94), shapes=[shape])
    vios = check_runway_profile(
        layout, end_grade_cap=None, check_curvature=False)
    grade_vios = [v for v in vios if v[0] == "grade"]
    assert grade_vios, f"expected an interior grade violation, got: {vios}"
    # 10 m over 500 m = 2.0 % against the 1.5 % uniform cap (the tiny excess
    # over 0.020 is the axis-tilt foreshortening of the 500 m station gap).
    assert abs(grade_vios[0][2] - 0.02) < 1e-4, grade_vios


def test_legacy_extreme_station_sampler_misses_interior_grade():
    """Pins the blind spot the fix closes: the SAME ring geometry read by the
    legacy cross-end path (``from_single_poly`` unset — extreme-station
    clustering) samples only the two physical ends, so the 2 % interior bump
    reads as a flat 0 % end→end profile and is NOT flagged.  This is what the
    validator did for every de-segmented runway before the per-station fix."""
    shape = _single_poly_runway_with_interior_bump()
    shape.from_single_poly = False
    layout = PavementLayout(icao="TEST", anchor=(35.22, -80.94), shapes=[shape])
    vios = check_runway_profile(
        layout, end_grade_cap=None, check_curvature=False)
    assert not [v for v in vios if v[0] == "grade"], (
        "legacy extreme-station sampler should have missed the interior "
        f"bump, but flagged: {vios}")
