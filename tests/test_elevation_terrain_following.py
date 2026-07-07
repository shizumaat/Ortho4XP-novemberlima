"""Regression tests for terrain-following elevation (user 2026-05-02).

The elevation solver is the route-profile one-solve on the unified grade
graph (``elevation_per_surface/route_profile/``): runway/CIFP corners and
tile-seam pins are HARD anchors, and every other pavement node is bounded
by the REACH-BAND law — a cap-Dijkstra from the runway anchors over the
spine graph (``building_feasibility.reach_band_unified``), so a surface
may climb toward the terrain only as fast as its route's grade caps allow.

These tests assert the pipeline lets pavement FOLLOW the terrain up to
that law-given ceiling instead of collapsing everything toward the runway
elevation (the historic over-flattening failure mode).

See ``docs/elevation_solver.md`` for the solver reference.

NOTE: the within-shape grade law is NOT hand-rolled here — it is covered
by ``grade_graph_validate.within_violations`` consumers
(``test_pavement_grade.test_cyxy_spine_zero_no_bowl``,
``test_single_graph_acceptance``); an old all-pair Euclidean audit that
contradicted the law (and collected zero items) was deleted 2026-07-05.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pytest

from conftest import xplane_available


_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


pytestmark = pytest.mark.skipif(
    not xplane_available(),
    reason="X-Plane install not found (set XPLANE_ROOT to override)",
)


def _build_layout(icao: str):
    # Shared session cache (conftest) — built once per airport per run.
    from conftest import cached_airport_layout
    return cached_airport_layout(icao)


def _shape_max_corner_alt(shape) -> Optional[float]:
    """Return the highest elevation that will actually be emitted to
    OSM for this shape.

    Mirrors ``layout.PavementLayout.to_osm`` field priority:
    ``altitude_high`` (sloped rect / runway) > ``node_altitudes``
    (junction) > ``altitude`` (flat).  Avoids reading stale fields
    that don't drive the rendered surface.
    """
    if shape.altitude_high is not None and shape.altitude_low is not None:
        return float(shape.altitude_high)
    if shape.node_altitudes:
        return max(float(a) for a in shape.node_altitudes)
    if shape.altitude is not None:
        return float(shape.altitude)
    return None


def _shapes_in_bbox(layout, x_lo: float, x_hi: float,
                    y_lo: float, y_hi: float):
    """Yield shapes whose polygon centroid falls inside the bbox."""
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
            continue
        c = s.polygon.centroid
        if x_lo <= c.x <= x_hi and y_lo <= c.y <= y_hi:
            yield s


# ── CYXY taxi E / SW apron — terrain follows DEM (user 2026-05-02) ─


def test_cyxy_taxi_e_south_apron_follows_terrain():
    """Taxi E at the south edge of CYXY's SW apron sits on natural
    terrain ~717 m (DEM truth).  The connected runway is at ~704 m
    (CIFP HARD).  The pavement must climb toward that terrain as far
    as the grade law allows — a collapse toward the runway elevation
    is the over-flattening failure mode this test guards.

    REQUIRED_M provenance (re-derived 2026-07-05): the historic
    714.0 m figure (user 2026-05-02) predates the REACH-BAND law —
    under the modern machinery no point may sit above its reach-band
    CEILING (cap-Dijkstra from the runway anchors over the unified
    spine graph, ``building_feasibility.reach_band_unified`` — the
    same band the solver seats against).  So the modern requirement
    is the LOWER of the two: the DEM-truth target 714 m, and the
    maximum band ceiling the law grants anywhere in the region.  If
    the band genuinely permits ≥ 714 m and the pavement still does
    not get there, this test is red honestly.

    Reference: ``docs/elevation_solver.md``.
    """
    layout = _build_layout("CYXY")

    # SW apron region (south edge): in CYXY meter coordinates the
    # SW apron sits south-east of the runway at roughly
    # (50..400, -1100..-700).  Taxi E's SW stub centroid is at
    # (124, -820); the DEM in this region is 715-718 m.
    bbox = (-100.0, 500.0, -1200.0, -700.0)
    # PAVEMENT only: clearance cuts are terrain FEATURES (flat shadows
    # of the edges they protect, no grade law) — since the part-30
    # ring-edge sweep they exist in this bbox too, and their terrain-
    # hugging altitudes must not satisfy a "pavement climbs" guard.
    candidates = [s for s in _shapes_in_bbox(layout, *bbox)
                  if s.role not in ("taxiway_clearance",
                                    "runway_clearance")]
    assert candidates, (
        "CYXY: no shapes found in SW apron bbox "
        f"x∈[{bbox[0]},{bbox[1]}] y∈[{bbox[2]},{bbox[3]}]")

    # The modern law's ceiling for the region: query the unified reach
    # band (built exactly the way the solver builds it — unified graph on
    # the final layout, runway anchors, cap-Dijkstra) at every candidate
    # ring vertex inside the bbox and take the max ceiling.
    from auto_patch import grade_graph as GG
    from auto_patch.elevation_per_surface.solver_primitives import (
        _build_node_list)
    from auto_patch.elevation_per_surface.building_feasibility import (
        reach_band_unified)
    _nodes, b2i = _build_node_list(layout)
    G = GG.build_unified_graph(layout, b2i)
    band = reach_band_unified(layout, G)
    ceilings = []
    for s in candidates:
        for (x, y) in s.polygon.exterior.coords:
            if not (bbox[0] <= x <= bbox[1] and bbox[2] <= y <= bbox[3]):
                continue
            fb = band(x, y)
            if fb is not None:
                ceilings.append(fb[1])
    assert ceilings, "CYXY: reach band returned no ceiling in the SW bbox"
    region_ceiling = max(ceilings)

    DEM_TRUTH_TARGET_M = 714.0          # user 2026-05-02, DEM ~715-718 here
    REQUIRED_M = min(DEM_TRUTH_TARGET_M, region_ceiling)

    best_alt = float("-inf")
    best_role = None
    for s in candidates:
        a = _shape_max_corner_alt(s)
        if a is None:
            continue
        if a > best_alt:
            best_alt = a
            best_role = s.role

    assert best_alt >= REQUIRED_M, (
        f"CYXY SW apron: highest pavement elevation in region is "
        f"{best_alt:.1f} m on a {best_role}, expected ≥ {REQUIRED_M:.1f} m "
        f"(= min(DEM-truth {DEM_TRUTH_TARGET_M:.1f}, reach-band ceiling "
        f"{region_ceiling:.1f})).  Likely over-flattening — the solver is "
        f"holding the region below what the reach-band law permits.")


# Within-shape (junction/apron) grade compliance is asserted through
# ``grade_graph_validate.within_violations`` consumers — see
# ``test_pavement_grade.py`` / ``test_single_graph_acceptance.py``.
