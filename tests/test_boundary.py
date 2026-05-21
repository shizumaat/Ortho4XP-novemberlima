"""Boundary ribbon / DEM-bridge regression guards.

The ``boundary_dem_bridge`` wedges bridge the airport-boundary ribbon
to the surrounding DEM.  Their OUTER edge sits on the airport
perimeter at the SAME altitude the ``airport_boundary`` ribbon assigns
at the co-located vertex (both use the asymmetric runway clamp,
``_runway_clamped_alt_at``).  When the bridge outer edge instead took
its altitude from the nearest pavement, it floated several metres above
the DEM-following ribbon at the shared XY and the OSM emitter rendered a
vertical wall between them — the CYXY perimeter spike/trench artifact
(138 walls up to 13.6 m; fixed in commit 480401f).

This is an integration test (builds CYXY) — the bridge altitude logic
has no unit-testable seam.  It guards the fix directly: at every shared
vertex between a bridge and the ribbon, their altitudes must agree.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

from conftest import xplane_available, xplane_root

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


pytestmark = pytest.mark.skipif(
    not xplane_available(),
    reason="X-Plane install not found (set XPLANE_ROOT to override)")


# Bridge-outer and ribbon vertices that land within this distance are
# the same perimeter point and must carry the same altitude.
_SHARED_XY_TOL_M = 0.5
# Max altitude disagreement at a shared vertex.  Co-located bridge/
# ribbon vertices agree to ~0.35 m at CYXY (both use the same clamp);
# the wall regression makes the outer edge float several metres above
# the ribbon, so a 1 m cap catches it with comfortable margin.
_WALL_TOL_M = 1.0


def _per_vertex_alts(shape):
    """Resolve a per-corner altitude for every exterior vertex of a
    BuiltShape (closing repeat dropped), or ``None`` when no altitude
    is known.  Mirrors the canonical altitude conventions:
      * ``node_altitudes`` — one value per vertex;
      * ``altitude`` — flat;
      * ``altitude_high``/``altitude_low`` — sloped 4-corner rect,
        sampled by projecting onto the high-mid → low-mid axis
        (``[H, L, L, H]`` corner order).
    """
    poly = shape.polygon
    if poly is None or poly.is_empty:
        return [], None
    coords = list(poly.exterior.coords)
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    n = len(coords)
    if n == 0:
        return coords, None
    if shape.node_altitudes and len(shape.node_altitudes) >= n:
        return coords, [float(shape.node_altitudes[k]) for k in range(n)]
    if shape.altitude is not None:
        return coords, [float(shape.altitude)] * n
    if (shape.altitude_high is not None
            and shape.altitude_low is not None and n == 4):
        H = float(shape.altitude_high)
        L = float(shape.altitude_low)
        hmx = 0.5 * (coords[0][0] + coords[3][0])
        hmy = 0.5 * (coords[0][1] + coords[3][1])
        lmx = 0.5 * (coords[1][0] + coords[2][0])
        lmy = 0.5 * (coords[1][1] + coords[2][1])
        ax, ay = lmx - hmx, lmy - hmy
        L2 = ax * ax + ay * ay
        if L2 < 1e-6:
            return coords, [0.5 * (H + L)] * n
        out = []
        for x, y in coords:
            t = ((x - hmx) * ax + (y - hmy) * ay) / L2
            t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
            out.append(H + t * (L - H))
        return coords, out
    return coords, None


def test_boundary_bridge_flush_with_ribbon_at_shared_vertices():
    """No vertical wall between a boundary DEM bridge and the airport
    boundary ribbon: every bridge vertex co-located with a ribbon
    vertex must agree in altitude within ``_WALL_TOL_M``.
    """
    from auto_patch.layout import ROLE_BOUNDARY
    from auto_patch.pipeline import build_airport_pavement

    layout = build_airport_pavement(
        "CYXY", xplane_root(), compute_elevations=True)

    ribbons, bridges = [], []
    for s in layout.shapes:
        if s.role != ROLE_BOUNDARY:
            continue
        if s.ref == "airport_boundary":
            ribbons.append(s)
        elif s.ref == "boundary_dem_bridge":
            bridges.append(s)
    if not bridges or not ribbons:
        pytest.skip("CYXY emitted no boundary bridge / ribbon to compare")

    rib_pts = []
    for s in ribbons:
        coords, alts = _per_vertex_alts(s)
        if alts is None:
            continue
        for (x, y), a in zip(coords, alts):
            rib_pts.append((x, y, a))

    tol2 = _SHARED_XY_TOL_M * _SHARED_XY_TOL_M
    worst = 0.0
    worst_xy = None
    n_pairs = 0
    for s in bridges:
        coords, alts = _per_vertex_alts(s)
        if alts is None:
            continue
        for (x, y), a in zip(coords, alts):
            best_d2 = tol2
            best_ribbon_alt = None
            for rx, ry, ra in rib_pts:
                d2 = (x - rx) ** 2 + (y - ry) ** 2
                if d2 <= best_d2:
                    best_d2 = d2
                    best_ribbon_alt = ra
            if best_ribbon_alt is None:
                continue
            n_pairs += 1
            dz = abs(a - best_ribbon_alt)
            if dz > worst:
                worst = dz
                worst_xy = (x, y, a, best_ribbon_alt)

    assert n_pairs >= 20, (
        f"too few shared bridge/ribbon vertices ({n_pairs}) — the "
        f"co-location check would be vacuous; geometry may have changed")
    assert worst <= _WALL_TOL_M, (
        f"boundary bridge floats {worst:.2f} m off the ribbon at a "
        f"shared vertex (cap {_WALL_TOL_M:.1f} m) — vertical-wall "
        f"regression.  Worst: bridge_alt={worst_xy[2]:.2f} vs "
        f"ribbon_alt={worst_xy[3]:.2f} at "
        f"({worst_xy[0]:.1f}, {worst_xy[1]:.1f}).  "
        f"Matched {n_pairs} shared vertices.")
