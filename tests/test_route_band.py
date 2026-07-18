"""ROUTE-BAND confirmation on THE unified grade graph G (handover item 1).

The solver bounds every airside node by the runway-reach band
(``building_feasibility.reach_band_unified``: a cap-Dijkstra over ``G.spine_adj``
from the runway anchors).  ``grade_graph_validate.route_band_violations`` is the
AS-BUILT confirmation of that bound on the SAME graph G — one graph, no separate
route-field.  A vertex outside its band is an infeasibility to ROOT-CAUSE (a
solver bug, a missing rule, or a rule needing adjustment), NEVER ignored:

  * ``ceil``  — above the reachable ceiling (steeper than cap from every runway);
  * ``floor`` — below the reachable floor;
  * ``pinned`` — the band is EMPTY (``floor > ceiling``): no compliant elevation
    exists at the vertex (mutually-unreachable runway anchors); fix is upstream.

SPJC is clean (0) and gates hard — a regression guard.  CYXY (aprons seated
above their reach ceiling — the A2-end apron / building over-pinning issue),
SPLP (taxi/junctions below the reach floor near the steep 02/20 runway) and HECA
(multi-runway empty bands) still carry violations; their zero-outcome is
xfail-tracked (the check still RUNS and the count is surfaced — not ignored), and
flips to XPASS the moment the solver/rule places every vertex in its band.
"""
from __future__ import annotations

import pytest

from conftest import cached_airport_layout

# Airports whose route-band outcome is currently RED — tracked, not ignored.
# Each is an infeasibility under root-cause (handover items 1–3); the xfail flips
# to XPASS the moment the solver/rule lands every airside vertex in its band.
# SPJC green since 2026-07-05; SPLP flipped to XPASS 2026-07-17 (the
# runway-datum reach exemption — vertices grading at cap from a local
# runway contact are the runway's own datum) and now gates hard.
_KNOWN_RED = {"CYXY", "HECA"}
_FIXTURES = ["SPJC", "CYXY", "SPLP", "HECA"]


def _param(icao):
    if icao in _KNOWN_RED:
        return pytest.param(icao, marks=pytest.mark.xfail(
            reason="route-band infeasibility under root-cause (handover items "
                   "1–3) — the check runs and surfaces the count; flips to "
                   "XPASS when every airside vertex lands in its reach band",
            strict=False))
    return icao


@pytest.mark.xdist_group("CYXY")   # reuse CYXY's already-built layout
def test_route_band_flags_cyxy_apron_ceiling():
    """ANTI-GAMING: the checker MUST flag CYXY's aprons seated above their reach
    ceiling, so the zero-gate cannot be faked by a no-op / over-weakened check."""
    from auto_patch.grade_graph_validate import route_band_violations
    v = route_band_violations(cached_airport_layout("CYXY"))
    assert v, "route_band_violations found nothing at CYXY — the checker is a no-op"
    assert any(t[1] == "ceil" for t in v), (
        "expected ceil (above-reach) violations at CYXY's hillside aprons; got "
        f"{[t[1] for t in v[:6]]}")


@pytest.mark.xdist_group("SPJC")   # reuse SPJC's already-built layout
def test_route_band_detects_injected_overshoot():
    """ANTI-GAMING: a vertex shoved well above its band MUST be flagged ``ceil``,
    so the gate cannot be quietly weakened (looser band, dropped vertices).  Uses
    SPJC (otherwise clean) so the flag can only come from the injected step.  The
    band is positional (runway anchors + spine geometry), so bumping junction
    elevations does not move the band — it only pushes the vertex out of it."""
    import copy
    from auto_patch.grade_graph_validate import route_band_violations
    from auto_patch.layout import ROLE_JUNCTION
    layout = copy.copy(cached_airport_layout("SPJC"))
    layout.shapes = [copy.copy(s) for s in layout.shapes]
    bumped = 0
    for s in layout.shapes:
        if (s.role == ROLE_JUNCTION and s.node_altitudes
                and s.polygon is not None and not s.polygon.is_empty):
            s.node_altitudes = [
                float(a) + 50.0 if a is not None else a for a in s.node_altitudes]
            bumped += 1
    assert bumped, "no junction with node_altitudes to perturb"
    v = route_band_violations(layout)
    assert any(t[1] == "ceil" for t in v), (
        "a +50 m junction overshoot was not flagged ceil — the band check is "
        "too weak; do not relax it to fake route-band=0")


def _raster_reach_band_on():
    """The active band producer — the runtime env overriding the config
    default (the resolution ``reach_band_unified`` uses)."""
    import os
    from auto_patch.config import RASTER_REACH_BAND
    env = os.environ.get("O4_RASTER_REACH_BAND")
    return (env == "1") if env is not None else bool(RASTER_REACH_BAND)


@pytest.mark.parametrize("icao", [_param(a) for a in _FIXTURES])
def test_route_band_zero(icao):
    """OUTCOME: zero route-band violations — every airside vertex sits inside the
    runway-reach band on the ONE graph G.  SPJC GREEN (hard regression guard); the
    others are xfail-tracked infeasibilities (the check runs, the count is
    surfaced — NOT ignored).

    RASTER-BAND RESIDUAL (Tier 3 wave 2b): under the deliberate raster
    reach-band replacement, junction ``route_band`` violations up to the
    documented grid-discretization bound (``RASTER_REACH_BAND_GRID_RESIDUAL_M``,
    cited in config.py — SPJC's one dense multi-anchor junction complex, worst
    0.228 m, EMITTED SURFACE unchanged) are the discretization residual, not a
    regression.  Anything larger, off a junction, or any emitted-surface defect
    still gates hard.  The anti-gaming injectors above (+50 m) exceed the bound
    by two orders of magnitude, so the gate cannot be faked."""
    from collections import Counter
    from auto_patch.grade_graph_validate import route_band_violations
    from auto_patch.config import RASTER_REACH_BAND_GRID_RESIDUAL_M
    layout = cached_airport_layout(icao)
    if not layout.shapes:
        pytest.skip(f"{icao}: no shapes built")
    v = route_band_violations(layout)
    if _raster_reach_band_on():
        # ``t = (excess_m, side, role, x, y, elev, lo, hi)``.
        v = [t for t in v
             if not (t[2] == "junction"
                     and t[0] <= RASTER_REACH_BAND_GRID_RESIDUAL_M)]
    cls = Counter(t[1] for t in v)
    assert not v, (
        f"{icao}: {len(v)} route-band violation(s) "
        f"(ceil={cls['ceil']}, floor={cls['floor']}, pinned={cls['pinned']}).  "
        f"worst: " + "; ".join(
            f"{t[1]} {t[0]:.1f}m {t[2]}@({t[3]:.0f},{t[4]:.0f})"
            for t in v[:4]))
