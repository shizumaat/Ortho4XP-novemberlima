"""The one-profile elevation solver (next-gen; docs/one_profile_solve.md).

A clean-room replacement for the legacy multi-pass cascade in
``unified_jacobi``: ONE solve owns every airside elevation.  Buildings seat flat
at their closest-to-DEM-in-band level, aprons grade closest-to-DEM within their
band, and the taxi route (rect ends + junction spine) carries the
runway→building climb as the smoothest cap-bounded surface between the anchors.

Entry point: :func:`solve_route_profile`.  ``solver.solve`` dispatches here when
``O4_ROUTE_PROFILE_SOLVE=1``; ``unified_jacobi`` is otherwise untouched.
"""
from __future__ import annotations

from .solve import solve_route_profile

__all__ = ["solve_route_profile"]
