"""Junction-refinement rule regression tests (user 2026-05-01).

One test per rule defined in
``/Users/noah/.claude/plans/kind-meandering-sifakis.md``.  Each test
counts violations and fails when the count exceeds a per-airport
regression baseline.  Initial baselines are zero — emission passes
should leave no violations.

Implementation phase order: Rule 2 → Rule 1 → Rule 4 → Rule 3.
Tests for un-implemented rules are placeholders that skip until
their emission pass lands.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from conftest import (
    airports_under_test, baseline_airports,
    is_tile_seam_vertex, xplane_available, xplane_root,
)


def _test_airports() -> list:
    """Union of baseline airports (always-run) + env-gated airports.
    See test_junction_invariants.py for rationale."""
    seen = set()
    out = []
    for ic in list(baseline_airports()) + list(airports_under_test()):
        if ic not in seen:
            seen.add(ic)
            out.append(ic)
    return out


_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


from auto_patch.config import ROUTE_ARC_SPINE as _ROUTE_ARC_SPINE

pytestmark = [
    pytest.mark.skipif(
        not xplane_available(),
        reason="X-Plane install not found (set XPLANE_ROOT to override)",
    ),
    # RECT-MODEL invariants: junction shapes here are the residue between
    # manufactured taxi rects.  Under the route-arc GLOBAL SLICE (default
    # 2026-07-02) no rects are emitted — faces are conformant by
    # construction (one polygonize arrangement) — so the rect-residue
    # rules no longer describe the geometry.  Kept for the legacy path
    # (O4_ROUTE_ARC_SPINE=0).
    pytest.mark.skipif(
        _ROUTE_ARC_SPINE,
        reason="rect-residue junction rules — no taxi rects under the "
               "route-arc global slice (O4_ROUTE_ARC_SPINE=1)",
    ),
]


# ── Per-airport regression baselines ──────────────────────────────
#
# Each baseline records the maximum permitted violation count for
# the corresponding rule at that airport.  When emission passes
# eliminate violations, lower the baseline.  Airports without an
# entry use the default zero ceiling.

# Universal zero — no per-airport baselines (user 2026-05-31).  Every
# junction vertex near the runway boundary must sit at a runway corner.
# (SPJC previously carried a 2-vertex baseline from Rule 5's push pass
# landing vertices in Rule 1's band — that is now a real failure to fix,
# not a tolerated exception.)
# SPJC=3 baseline (2026-06-20, user-accepted as visually fine in X-Plane):
# 3 junction vertices kiss a runway boundary but are orphans (not shared with
# a runway segment endpoint) — #229 v7 / #230 v2 (same pt, 82.6 m orphan) and
# #264 v5 (5.0 m orphan), from the spine-slice/cap geometry.  ⚠ candidates to
# drive back to 0; not seam-related.
RULE1_REGRESSION_BASELINE: Dict[str, int] = {"SPJC": 3}
# (session 55) Rule 2 / RULE2_REGRESSION_BASELINE removed
# (test_junction_no_long_edge_proximity): it flagged a junction vertex
# within SLOPING_EDGE_SNAP_M (20 m) PERPENDICULAR of a sloping rect's long
# edge — a PROXIMITY proxy.  Per user 2026-05-29: mere proximity is not a
# problem.  What actually breaks a sloping rect is placing a node ON its
# edge (adding a vertex / a mid-edge elevation); a junction running close
# alongside a rect is fine as long as it shares only the rect's CORNER
# nodes (matching elevations).  That real invariant is tested directly by
# `test_no_vertex_on_sloping_rect_edge` (vertex on a sloping edge INTERIOR,
# corners exempt) + the grade/step tests (shared-corner elevation match),
# so the proximity rule only produced false positives (HECA junctions 213/
# 250/357 running 2-14 m alongside a stub) and was retired.
# (session 51) RULE4_REGRESSION_BASELINE removed: paired with the
# retired `_split_narrow_necks` pass (see test_no_narrow_neck_junctions
# removal note below + STATUS.md).
A4_BASELINE: Dict[str, int] = {
    # Invariant A4: every junction/apron vertex lies INSIDE (or on the
    # boundary of) pav_union.  Zero per-airport tolerance under the
    # single-solve / no-halo model.  The OLD test asserted the inverse
    # (vertices OUTSIDE by `PAVEMENT_OUTWARD_OFFSET_M`, the 2-solve
    # "elevation-smoothing halo") and carried a 240-vertex SPJC
    # baseline — that test was sense-inverted vs A4 and has been
    # rewritten in `test_junction_vertices_outside_pavement` below.
    # SPJC=2 baseline (2026-06-20, user-accepted as visually fine): 2
    # junction vertices (#229 v7 / #230 v2, same pt) sit 0.79 m outside the
    # pavement union, from the spine-slice/cap geometry.  ⚠ candidate to fix.
    "SPJC": 2,
}


def _build_layout(icao: str):
    # Shared session cache (conftest) — built once per airport per run.
    from conftest import cached_airport_layout
    return cached_airport_layout(icao)


def _rect_sloping_edges_from_shape(shape) -> List[
        Tuple[Tuple[float, float], Tuple[float, float]]]:
    """The two SLOPING edges of a 4-corner rect — edges parallel
    to ``source_axis`` (where altitude varies linearly).  Per user
    2026-05-02 clarification: 'long' vs 'short' was misleading;
    what matters is direction of slope.  Falls back to longest-2
    if source_axis is missing.
    """
    poly = shape.polygon
    coords = list(poly.exterior.coords)
    if not coords:
        return []
    if coords[0] == coords[-1]:
        coords = coords[:-1]
    if len(coords) != 4:
        return []
    edges = [(coords[i], coords[(i + 1) % 4]) for i in range(4)]
    sa = getattr(shape, "source_axis", None)
    if sa is not None and not sa.is_empty:
        ax_pts = list(sa.coords)
        if len(ax_pts) >= 2:
            axdx = ax_pts[-1][0] - ax_pts[0][0]
            axdy = ax_pts[-1][1] - ax_pts[0][1]
            axlen = math.hypot(axdx, axdy)
            if axlen >= 1e-6:
                aux, auy = axdx / axlen, axdy / axlen
                dots = []
                for a, b in edges:
                    ex, ey = b[0] - a[0], b[1] - a[1]
                    elen = math.hypot(ex, ey)
                    if elen < 1e-6:
                        dots.append(0.0)
                        continue
                    dots.append(abs(ex * aux + ey * auy) / elen)
                sloping_idx = sorted(
                    range(4), key=lambda i: -dots[i])[:2]
                return [edges[i] for i in sloping_idx]
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1])
               for a, b in edges]
    long_idx = sorted(range(4), key=lambda i: -lengths[i])[:2]
    return [edges[i] for i in long_idx]


# Backward-compat alias.
def _rect_long_edges_from_poly(poly):
    """Legacy length-based; new code should use
    ``_rect_sloping_edges_from_shape(shape)`` to get the correct
    sloping edges via source_axis."""
    coords = list(poly.exterior.coords)
    if not coords:
        return []
    if coords[0] == coords[-1]:
        coords = coords[:-1]
    if len(coords) != 4:
        return []
    edges = [(coords[i], coords[(i + 1) % 4]) for i in range(4)]
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1])
               for a, b in edges]
    long_idx = sorted(range(4), key=lambda i: -lengths[i])[:2]
    return [edges[i] for i in long_idx]


def _rect_corners(poly) -> List[Tuple[float, float]]:
    coords = list(poly.exterior.coords)
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    return [(c[0], c[1]) for c in coords]


def _point_segment_distance(px, py, ax, ay, bx, by) -> float:
    """Distance from point (px, py) to segment (a, b) (clamped to
    segment endpoints)."""
    dx = bx - ax
    dy = by - ay
    seg2 = dx * dx + dy * dy
    if seg2 < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / seg2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    fx = ax + t * dx
    fy = ay + t * dy
    return math.hypot(px - fx, py - fy)


def _point_perp_dist_within_segment(px, py, ax, ay, bx, by):
    """Perpendicular distance to the long edge LINE, but only when
    the foot falls strictly within the segment (0 < t < 1).  Returns
    ``None`` for projections past either endpoint — vertices reaching
    toward the rect's short-end corner are allowed even though
    they're physically close to the long-edge endpoint.
    """
    dx = bx - ax
    dy = by - ay
    seg2 = dx * dx + dy * dy
    if seg2 < 1e-9:
        return None
    t = ((px - ax) * dx + (py - ay) * dy) / seg2
    if t <= 0.0 or t >= 1.0:
        return None
    fx = ax + t * dx
    fy = ay + t * dy
    return math.hypot(px - fx, py - fy)


# ── Rule 2: long-edge corner snap ─────────────────────────────────


SLOPING_RECT_ROLES = ("primary_parallel", "secondary_parallel",
                      "stub", "cross_connector")


# test_junction_no_long_edge_proximity (Rule 2) removed in session 55 —
# see the RULE2_REGRESSION_BASELINE removal note above.  The real
# "no node on a sloping rect's edge" invariant lives in
# test_pavement_geometry.test_no_vertex_on_sloping_rect_edge.


# ── Rule 1, Rule 3, Rule 4 ────────────────────────────────────────
# Placeholder tests — enable once each rule's emission pass lands.


@pytest.mark.parametrize("icao", _test_airports())
def test_junction_runway_node_sharing(icao):
    """Rule 1: each junction vertex on a runway boundary span must
    coincide EXACTLY (within ``SHARED_VERTEX_TOL_M``) with some
    runway vertex.  No orphan junction vertices floating between
    runway nodes.  See plan §Rule 1.
    """
    from auto_patch.config import RUNWAY_BOUNDARY_TOL_M
    from auto_patch.layout import SHARED_VERTEX_TOL_M

    layout = _build_layout(icao)

    # Each segment paired with its endpoint vertices — orphan check
    # snaps to nearest segment's endpoint, never crosses to a
    # different segment.
    rwy_segs: List[Tuple[float, float, float, float,
                         Tuple[float, float], Tuple[float, float]]] = []
    for s in layout.shapes:
        if s.role != "runway":
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        c = list(s.polygon.exterior.coords)
        if c and c[0] == c[-1]:
            c = c[:-1]
        m = len(c)
        for i in range(m):
            ax, ay = c[i]
            bx, by = c[(i + 1) % m]
            rwy_segs.append(
                (float(ax), float(ay), float(bx), float(by),
                 (float(ax), float(ay)), (float(bx), float(by))))

    if not rwy_segs:
        pytest.skip(f"{icao}: no runway shapes")

    boundary_tol = RUNWAY_BOUNDARY_TOL_M
    vertex_tol = SHARED_VERTEX_TOL_M

    violations: List[str] = []
    for s_idx, s in enumerate(layout.shapes):
        if s.role != "junction":
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        c = list(s.polygon.exterior.coords)
        if c and c[0] == c[-1]:
            c = c[:-1]
        for v_idx, (vx, vy) in enumerate(c):
            best_seg_d = float("inf")
            best_endpoints: Tuple[Tuple[float, float],
                                  Tuple[float, float]] = ((0, 0), (0, 0))
            for ax, ay, bx, by, c1, c2 in rwy_segs:
                d = _point_segment_distance(vx, vy, ax, ay, bx, by)
                if d < best_seg_d:
                    best_seg_d = d
                    best_endpoints = (c1, c2)
                    if best_seg_d <= 1e-6:
                        break
            if best_seg_d > boundary_tol:
                continue
            # Vertex on runway boundary — must coincide with one of
            # the closest segment's two endpoints.
            ep1, ep2 = best_endpoints
            d1 = math.hypot(vx - ep1[0], vy - ep1[1])
            d2 = math.hypot(vx - ep2[0], vy - ep2[1])
            if min(d1, d2) <= vertex_tol:
                continue
            violations.append(
                f"junction#{s_idx} vertex#{v_idx} at "
                f"({vx:.2f},{vy:.2f}) is {best_seg_d:.2f}m from "
                f"runway boundary but {min(d1, d2):.2f}m from the "
                f"nearest segment endpoint (orphan)")

    baseline = RULE1_REGRESSION_BASELINE.get(icao, 0)
    if len(violations) > baseline:
        msg = (f"{icao}: Rule 1 violations = {len(violations)} > "
               f"baseline {baseline}\nFirst 10:\n  "
               + "\n  ".join(violations[:10]))
        if len(violations) > 10:
            msg += f"\n  ... and {len(violations) - 10} more"
        pytest.fail(msg)


# (session 51) `test_large_junction_axis_aligned_borders` was REMOVED:
# it policed interior cut-line edges from the OLD
# `_decompose_polygon_with_holes` flow.  Under invariants A1/A2,
# junctions are defined as the residue `pav_union − rects` and have NO
# synthetic interior cut lines to align — there is no invariant that
# requires their interior edges to be runway-parallel-or-perpendicular.
# The old SPJC=20 / CYXY=88 baselines confirmed it was policing
# implementation detail, not geometry truth.  See docs/pipeline_invariants.md.


# (session 51) test_no_narrow_neck_junctions REMOVED — paired with the retired
#  pass.  Neck-splitting is now handled by
# pavement/apron_necks.py::split_polygon_at_necks (medial-axis traced).


@pytest.mark.parametrize("icao", _test_airports())
def test_junction_vertices_outside_pavement(icao):
    """Invariant A4 (single-solve model, see docs/pipeline_invariants.md):
    every junction/apron vertex lies INSIDE (or on the boundary of)
    ``pav_union`` — it is a VIOLATION for any junction vertex to sit
    outside the pavement footprint.

    The kept name (``..._outside_pavement``) reflects the violation we
    test for: junction vertices that ended up OUTSIDE the pavement.  An
    earlier ``Rule 5`` revision of this test asserted the inverse — that
    vertices must be outside by ``PAVEMENT_OUTWARD_OFFSET_M`` to form an
    "elevation-smoothing halo".  That halo is a 2-solve-era artifact
    superseded by the per-surface solver; under the new invariants A4
    a vertex outside the footprint has no source shape, breaks node
    sharing, and risks rendering off-pavement at the seam.
    """
    from auto_patch.junction_rules import PAVEMENT_INSIDE_TOL_M
    from auto_patch.layout import SHARED_VERTEX_TOL_M
    from shapely.geometry import Point as _Point

    layout = _build_layout(icao)
    pav_union = getattr(layout, "_source_pav_union", None)
    if pav_union is None or pav_union.is_empty:
        pytest.skip(f"{icao}: layout has no _source_pav_union")

    # Anchor edges for exemption — a junction vertex shared with a
    # rect/runway/terminal edge is legitimately on the pavement
    # boundary (it IS the boundary on that segment).
    anchor_segs: List[Tuple[float, float, float, float]] = []
    for s in layout.shapes:
        if s.role not in ("primary_parallel", "secondary_parallel",
                          "stub", "cross_connector",
                          "runway", "building"):
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        c = list(s.polygon.exterior.coords)
        if c and c[0] == c[-1]:
            c = c[:-1]
        m = len(c)
        for i in range(m):
            ax, ay = c[i]
            bx, by = c[(i + 1) % m]
            anchor_segs.append((float(ax), float(ay),
                                float(bx), float(by)))

    # Bridge-shared exemption — boundary_dem_bridge polygons are the
    # transition strips between the pavement ribbon and DEM terrain;
    # their OUTER ring legitimately sits outside pav_union, and where a
    # junction edge meets a bridge those shared vertices follow the
    # bridge into the outside.  A junction vertex coincident with any
    # bridge vertex (within SHARED_VERTEX_TOL_M) is therefore
    # legitimately outside.
    bridge_pts: List[Tuple[float, float]] = []
    for s in layout.shapes:
        if s.role != "boundary" or s.ref != "boundary_dem_bridge":
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        c = list(s.polygon.exterior.coords)
        if c and c[0] == c[-1]:
            c = c[:-1]
        for vx, vy in c:
            bridge_pts.append((float(vx), float(vy)))

    violations: List[str] = []
    for s_idx, s in enumerate(layout.shapes):
        if s.role != "junction":
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        c = list(s.polygon.exterior.coords)
        if c and c[0] == c[-1]:
            c = c[:-1]
        for v_idx, (vx, vy) in enumerate(c):
            # Anchor exemption.
            on_anchor = False
            for ax, ay, bx, by in anchor_segs:
                if _point_segment_distance(
                        vx, vy, ax, ay, bx, by) <= SHARED_VERTEX_TOL_M:
                    on_anchor = True
                    break
            if on_anchor:
                continue
            # Tile-cut seam vertex sits ~half_width off the integer
            # line and is anchored by tile_cut, not free to push
            # outside the pavement.
            if is_tile_seam_vertex(layout, vx, vy):
                continue
            # Bridge-shared vertices may sit outside pav_union — bridges
            # connect inner ribbon to outer DEM terrain by design.
            on_bridge = False
            for bx, by in bridge_pts:
                if (vx - bx) * (vx - bx) + (vy - by) * (vy - by) \
                        <= SHARED_VERTEX_TOL_M * SHARED_VERTEX_TOL_M:
                    on_bridge = True
                    break
            if on_bridge:
                continue
            # A4: vertex must be INSIDE the pavement union, OR within
            # PAVEMENT_INSIDE_TOL_M of its boundary (i.e. ON the
            # boundary line, modulo float tolerance).
            p = _Point(vx, vy)
            if pav_union.contains(p):
                continue
            d = p.distance(pav_union.boundary)
            if d <= PAVEMENT_INSIDE_TOL_M:
                continue
            violations.append(
                f"junction#{s_idx} vertex#{v_idx} at "
                f"({vx:.2f},{vy:.2f}) is OUTSIDE pavement at "
                f"distance {d:.2f}m from boundary")

    baseline = A4_BASELINE.get(icao, 0)
    if len(violations) > baseline:
        msg = (f"{icao}: A4 violations = {len(violations)} > "
               f"baseline {baseline}\nFirst 10:\n  "
               + "\n  ".join(violations[:10]))
        if len(violations) > 10:
            msg += f"\n  ... and {len(violations) - 10} more"
        pytest.fail(msg)
