"""Per-surface elevation solver — top-level entry point.

Delegates to ``unified_jacobi.solve``.  The DEM + tile coords are
accepted for API parity with the legacy unified solver and for
future use (per-vertex DEM seeding) but are currently unused: the
solver warm-starts soft nodes from existing layout altitudes
(rect altitude_high/low, junction node_altitudes, terminal altitude)
which were already populated upstream from DEM.

See ``unified_jacobi`` for the per-axis grade rule and the
three-change derivation from the legacy unified solver.
"""
from __future__ import annotations

import os as _os

from .unified_jacobi import solve as _jacobi_solve

# O4_ROUTE_PROFILE_SOLVE routes the elevation solve through the next-gen
# one-profile solver (``route_profile`` package, docs/one_profile_solve.md)
# instead of the legacy ``unified_jacobi`` multi-pass cascade.  Default ON in dev
# (user 2026-06-25: build + test in X-Plane while we drive CYXY to 0 violations
# and recut fixtures).  Set ``O4_ROUTE_PROFILE_SOLVE=0`` to fall back to the
# legacy solver (still live; the new solver also drives the gated airside-freeze
# in the post-solve tile cut).
ROUTE_PROFILE_SOLVE = _os.environ.get("O4_ROUTE_PROFILE_SOLVE", "1") == "1"


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
    if ROUTE_PROFILE_SOLVE:
        from .route_profile import solve_route_profile
        solve_route_profile(layout, icao, dem=dem,
                            tile_lat=tile_lat, tile_lon=tile_lon)
        return
    _jacobi_solve(layout, icao, dem=dem,
                   tile_lat=tile_lat, tile_lon=tile_lon)
