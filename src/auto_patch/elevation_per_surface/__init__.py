"""Per-surface elevation solver (user 2026-05-02 / 2026-05-03).

Replacement for the legacy unified Laplacian solver in
``auto_patch.elevation._solve_pavement_elevations_unified``.

The legacy solver treats all pavement as one connected graph with
per-edge grade caps and propagates runway HARD anchors through
every connected vertex.  This violates the per-axis FAA grade rule:

* Taxi rect grade applies along source_axis only — never
  perpendicular to an unrelated surface like a parallel runway.
* Junction / apron grade applies in any direction (multi-directional)
  within the polygon's surface, but not across to a different shape.
* Terminals are flat; aprons follow terrain at 1.5 % grade.
* Only CIFP runway corners are immutable HARD anchors — terminals,
  aprons, and taxi rects can all adjust.

The implementation lives in the ``route_profile`` package
(``solve_route_profile``) — one elevation profile solved on the single
unified grade graph — with the elevation-neutral primitives (node list,
DEM seed/sample, within-shape constraint + level-coupling graph, runway
node/edge sets, writeback) in ``solver_primitives``.  See
``docs/elevation_solver.md`` and ``docs/one_profile_solve.md``.
"""
from .solver import solve

__all__ = ["solve"]
