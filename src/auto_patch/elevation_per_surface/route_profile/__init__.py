"""The one-profile elevation solver (next-gen; docs/one_profile_solve.md).

The solver: ONE solve owns every airside elevation.  Buildings seat flat
at their closest-to-DEM-in-band level, aprons grade closest-to-DEM within their
band, and the taxi route (rect ends + junction spine) carries the
runway→building climb as the smoothest cap-bounded surface between the anchors.

Entry point: :func:`solve_route_profile`.  ``solver.solve`` dispatches here
unconditionally; the elevation-neutral primitives it uses live in
``elevation_per_surface.solver_primitives``.
"""
from __future__ import annotations

from .solve import solve_route_profile

__all__ = ["solve_route_profile"]
