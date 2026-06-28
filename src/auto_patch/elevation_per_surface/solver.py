"""Per-surface elevation solver — top-level entry point.

Delegates to ``route_profile.solve_route_profile`` (the one-profile solver,
docs/one_profile_solve.md).  The DEM + tile coords are accepted for API parity
and for per-vertex DEM seeding of SOFT nodes.

The elevation-neutral primitives the solver uses (node list, DEM seed/sample,
within-shape constraint + level-coupling graph, runway node/edge sets,
writeback, report) live in ``solver_primitives`` — extracted from the former
legacy solver cascade, which was deleted (M2 cleanup, see
docs/cleanup_consolidation_plan.md).
"""
from __future__ import annotations


def solve(layout, icao: str,
          dem=None, tile_lat: int = 0, tile_lon: int = 0) -> None:
    """Per-surface phased elevation solve.  Mutates ``layout`` in
    place: writes ``altitude_high``/``altitude_low`` on rects,
    ``node_altitudes`` on junctions, ``altitude`` on terminals and
    aprons.  Runway segments (HARD anchors) are left untouched.

    When ``dem`` is supplied, SOFT nodes are seeded from per-vertex
    DEM samples — necessary so taxi rects/junctions reach the
    DEM-driven elevations the user expects (e.g. CYXY taxi E sits
    on terrain ~717 m, not pulled down to the 705 m runway).
    """
    from .route_profile import solve_route_profile
    solve_route_profile(layout, icao, dem=dem,
                        tile_lat=tile_lat, tile_lon=tile_lon)
