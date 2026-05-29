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


_requires_xplane = pytest.mark.skipif(
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


@_requires_xplane
def test_boundary_bridge_flush_with_ribbon_at_shared_vertices():
    """No vertical wall between a boundary DEM bridge and the airport
    boundary ribbon: every bridge vertex co-located with a ribbon
    vertex must agree in altitude within ``_WALL_TOL_M``.
    """
    from auto_patch.layout import ROLE_BOUNDARY
    from conftest import cached_airport_layout

    # Shared session cache — built once per airport per run.
    layout = cached_airport_layout("CYXY")

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


@_requires_xplane
def test_no_shape_crosses_airport_boundary():
    """Invariant (user 2026-05-22): no emitted pavement shape may cross
    the airport boundary (apt.dat row-130).  The boundary ribbon lies
    entirely inside the line and pavement is clipped back to the ribbon's
    inner edge, so every non-boundary shape stays within row-130."""
    from auto_patch.boundary import find_boundary_crossings
    from conftest import cached_airport_layout

    # Shared session cache — built once per airport per run.
    layout = cached_airport_layout("CYXY")
    if layout.airport_boundary is None or layout.airport_boundary.is_empty:
        pytest.skip("CYXY has no usable row-130 boundary to gate against")
    crossings = find_boundary_crossings(layout)
    assert not crossings, (
        f"{len(crossings)} shape(s) cross the airport boundary: "
        + ", ".join(
            f"{s.role}/{s.ref} "
            f"({s.polygon.difference(layout.airport_boundary).area:.1f} m²)"
            for s in crossings[:8]))


@_requires_xplane
def test_ribbon_flush_with_pavement_at_shared_vertices():
    """No vertical wall between the boundary ribbon and the pavement it
    abuts: the ribbon yields its elevation to pavement at every shared
    seam vertex (``_conform_ribbon_to_pavement_seam``), so co-located
    ribbon/pavement vertices agree in altitude within ``_WALL_TOL_M``."""
    from auto_patch.layout import ROLE_BOUNDARY
    from conftest import cached_airport_layout

    # Shared session cache — built once per airport per run.
    layout = cached_airport_layout("CYXY")

    rib_pts, pav_pts = [], []
    for s in layout.shapes:
        coords, alts = _per_vertex_alts(s)
        if alts is None:
            continue
        if s.role == ROLE_BOUNDARY:
            if s.ref == "airport_boundary":
                rib_pts.extend(zip(coords, alts))
        else:
            pav_pts.extend(zip(coords, alts))
    if not rib_pts or not pav_pts:
        pytest.skip("CYXY emitted no ribbon / pavement to compare")

    tol2 = _SHARED_XY_TOL_M * _SHARED_XY_TOL_M
    worst = 0.0
    worst_xy = None
    n_pairs = 0
    for (x, y), a in rib_pts:
        best_d2 = tol2
        best_pav_alt = None
        for (px, py), pa in pav_pts:
            d2 = (x - px) ** 2 + (y - py) ** 2
            if d2 <= best_d2:
                best_d2 = d2
                best_pav_alt = pa
        if best_pav_alt is None:
            continue
        n_pairs += 1
        dz = abs(a - best_pav_alt)
        if dz > worst:
            worst = dz
            worst_xy = (x, y, a, best_pav_alt)

    if n_pairs < 10:
        pytest.skip(
            f"too few shared ribbon/pavement vertices ({n_pairs}) at CYXY")
    assert worst <= _WALL_TOL_M, (
        f"ribbon floats {worst:.2f} m off abutting pavement at a shared "
        f"vertex (cap {_WALL_TOL_M:.1f} m) — seam-wall regression.  "
        f"Worst: ribbon_alt={worst_xy[2]:.2f} vs pav_alt={worst_xy[3]:.2f} "
        f"at ({worst_xy[0]:.1f}, {worst_xy[1]:.1f}).  "
        f"Matched {n_pairs} shared vertices.")


# ──────────────────────────────────────────────────────────────────────
# _node_altitudes_from_segment_slope — pure helper (no X-Plane build)
# ──────────────────────────────────────────────────────────────────────
# A boundary ribbon strip is built as a 4-corner sloped quad
# (altitude_high/low).  When it partially overlaps pavement it gets
# trimmed (poly.difference) into a NON-quad — which is invalid for
# Ortho4XP's altitude_high/low encoder.  The emitter converts such a
# strip to per-vertex node_altitudes, preserving the along-perimeter
# slope by linear eh->el interpolation.  These tests pin that helper.
import auto_patch.elevation  # noqa: F401  (resolves boundary import order)
from auto_patch.boundary import _node_altitudes_from_segment_slope


def test_segment_slope_interpolates_along_high_low_axis():
    # Slope from (0,0) at 60 m down to (100,0) at 50 m; flat across width.
    open_ring = [(0.0, 0.0), (100.0, 0.0), (100.0, 10.0),
                 (50.0, 10.0), (0.0, 10.0)]
    alts = _node_altitudes_from_segment_slope(
        open_ring, (0.0, 0.0), (100.0, 0.0), 60.0, 50.0)
    # One value per vertex + closing repeat.
    assert alts == [60.0, 50.0, 50.0, 55.0, 60.0, 60.0]
    # It actually slopes (not flattened).
    assert max(alts) - min(alts) == pytest.approx(10.0)


def test_segment_slope_clamps_past_segment_ends():
    # Projections beyond the segment clamp to the end altitudes.
    open_ring = [(-50.0, 0.0), (150.0, 0.0), (50.0, 5.0)]
    alts = _node_altitudes_from_segment_slope(
        open_ring, (0.0, 0.0), (100.0, 0.0), 60.0, 50.0)
    assert alts[0] == 60.0   # before high end → eh
    assert alts[1] == 50.0   # past low end → el
    assert alts[2] == 55.0   # midpoint


def test_segment_slope_degenerate_axis_uses_high():
    # Zero-length axis (high == low) → every vertex gets eh.
    open_ring = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]
    alts = _node_altitudes_from_segment_slope(
        open_ring, (5.0, 5.0), (5.0, 5.0), 42.0, 41.0)
    assert alts == [42.0, 42.0, 42.0, 42.0]


def test_segment_slope_empty_ring():
    assert _node_altitudes_from_segment_slope(
        [], (0.0, 0.0), (1.0, 0.0), 1.0, 0.0) == []
