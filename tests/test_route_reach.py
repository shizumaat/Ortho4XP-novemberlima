"""ROUTE-REACH validator (user 2026-06-26).

A no-building apron must get a single base elevation that is within-cap reachable
via ALL the taxiways that feed it.  CYXY's large west apron (~66 000 m², no
buildings) is fed by TX1 (685.2), TX2 (690.2) and TX3 (677.0) — TX2 vs TX3 differ
by 13 m over ~480 m = 2.76 %, far over the 1 % apron cap, so NO cap-compliant
surface connects them and the apron is forced to a steep, partly-unreachable
elevation.  ``grade_graph_validate.route_reach_violations`` catches this; the fix
(make the feeder taxiways converge toward a shared reachable level) is tracked by
``test_cyxy_route_reach_zero``, RED until it lands.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("O4_ROUTE_PROFILE_SOLVE", "1")


def _cyxy():
    from conftest import cached_airport_layout
    return cached_airport_layout("CYXY")


def test_route_reach_detects_incompatible_apron():
    """ANTI-GAMING: the validator must FLAG the west apron whose feeder taxiways
    arrive at mutually unreachable elevations (so the zero-gate cannot be faked by
    a no-op check)."""
    from auto_patch.grade_graph_validate import route_reach_violations
    v = route_reach_violations(_cyxy())
    assert v, "route_reach_violations found nothing — the checker is a no-op"
    # the big west apron is around (298, 342) in local meters, worst pair ~2.7%.
    big = [x for x in v if abs(x[5] - 298) < 80 and abs(x[6] - 342) < 80]
    assert big, (
        f"the incompatible west apron was not flagged; got "
        f"{[(round(p, 2), round(x), round(y)) for (p, _c, _d, _r, _s, x, y) in v]}")


@pytest.mark.xfail(reason="feeder-taxiway convergence not built yet — the no-"
                          "building apron's taxiways still arrive incompatible",
                   strict=False)
def test_cyxy_route_reach_zero():
    """OUTCOME: zero route-reach violations — every no-building apron has a single
    base elevation reachable via all its feeder taxiways.  RED until the taxiways
    converge toward a shared reachable level."""
    from auto_patch.grade_graph_validate import route_reach_violations
    v = route_reach_violations(_cyxy())
    assert not v, (
        f"{len(v)} route-reach violation(s) — a no-building apron's feeders are "
        f"incompatible.  worst: "
        f"{[(round(p, 2), round(x), round(y)) for (p, _c, _d, _r, _s, x, y) in v[:4]]}")
