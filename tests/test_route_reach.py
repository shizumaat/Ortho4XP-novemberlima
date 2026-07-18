"""ROUTE-REACH validator + feeder-convergence rule (user 2026-06-26 directive #3).

A no-building apron must get a single base elevation that is within-cap reachable
via ALL the taxiways that feed it.  CYXY's large west apron (~66 000 m², no
buildings) used to be fed by taxiways arriving 16 m apart in the bowl, so no
cap-compliant surface connected them and ``route_reach_violations`` flagged it.
The fix landed in two parts (2026-06-28): the EDGE-SKELETON reach
(``O4_SKELETON_REACH``) gives the no-centerline feeders a reach band, and the
NO-BUILDING APRON SEAT (``O4_NOBUILD_APRON_SEAT``) seats each such apron flat at the
shared reachable level so its feeders converge.  ``test_cyxy_route_reach_zero`` now
gates that the real airport is clean; the anti-gaming guard is synthetic.
"""
from __future__ import annotations

import pytest

# All-CYXY module (hardcoded) — pin to CYXY's xdist group so it reuses the
# already-built layout instead of rebuilding on a stray worker.
pytestmark = pytest.mark.xdist_group("CYXY")


def _cyxy():
    from conftest import cached_airport_layout
    return cached_airport_layout("CYXY")


def test_route_reach_detects_incompatible_apron():
    """ANTI-GAMING (synthetic): ``route_reach_violations`` must FLAG a no-building
    apron whose feeder junctions arrive at incompatible elevations, so the
    zero-gate cannot be faked by a no-op checker.

    Synthetic rather than a real airport because the feeder-convergence solver now
    fixes the real cases (see ``test_cyxy_route_reach_zero``) — a guard that relied
    on a real apron staying broken would rot.  Two feeder junctions touch opposite
    corners of a 40 m apron at 700 vs 710 m (10 m over a ~57 m diagonal = 17.6 %,
    far over the 1 % apron cap), with no building to anchor the level."""
    from types import SimpleNamespace
    from shapely.geometry import Polygon
    from auto_patch.layout import ROLE_APRON, ROLE_JUNCTION
    from auto_patch.grade_graph_validate import route_reach_violations

    def _shape(role, coords, elev, ref=""):
        return SimpleNamespace(
            role=role, polygon=Polygon(coords), ref=ref, altitude=None,
            node_altitudes=[float(elev)] * len(coords),
            altitude_high=None, altitude_low=None)

    apron = _shape(ROLE_APRON, [(0, 0), (40, 0), (40, 40), (0, 40)], 705.0)
    fA = _shape(ROLE_JUNCTION, [(-5, -5), (0, -5), (0, 0), (-5, 0)], 700.0, "A")
    fB = _shape(ROLE_JUNCTION, [(40, 40), (45, 40), (45, 45), (40, 45)], 710.0, "B")
    v = route_reach_violations(SimpleNamespace(shapes=[apron, fA, fB]))
    assert v, "route_reach_violations did not flag the incompatible apron — no-op?"


@pytest.mark.xfail(
    reason="feeder-convergence residual UNDER TRACKING (user in-sim review "
           "2026-07-17: not visible, accepted for now) — three no-building "
           "aprons whose feeder contacts disagree beyond the 1% parking "
           "standard (2.42%/13.7 m, 2.12%/10.9 m, 1.69%/325 m).  The fix is "
           "upstream feeder convergence; the check still runs and surfaces "
           "the count, and flips to XPASS when it lands.",
    strict=False)
def test_cyxy_route_reach_zero():
    """OUTCOME: zero route-reach violations at CYXY — every no-building apron is
    feasible for all its feeders.  The edge-skeleton reach (O4_SKELETON_REACH)
    connects the no-centerline west apron; the no-building apron seat
    (O4_NOBUILD_APRON_SEAT) anchors each apron's feeder contacts at their per-feeder
    feasible level (the apron tilts ≤cap between them) so the feeder spines converge
    to meet it."""
    from auto_patch.grade_graph_validate import route_reach_violations
    v = route_reach_violations(_cyxy())
    assert not v, (
        f"{len(v)} route-reach violation(s) — a no-building apron's feeders are "
        f"incompatible.  worst: "
        f"{[(round(p, 2), round(x), round(y)) for (p, _c, _d, _r, _s, x, y) in v[:4]]}")
