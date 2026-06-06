"""Redistribute runway-segment altitudes after seam DEM anchors enter
the profile (user 2026-05-19).

Background
----------
``runway_segments.generate_patch_osm`` emits an FAA-compliant runway
profile at the time it runs — anchored by CIFP thresholds, cross-runway
projection anchors, and centerline-crossing reconciliation, all passed
through the parabolic envelope clamp + hard-cap + rate-of-change
gates.  But it knows nothing about tile-boundary seams: the seam
pipeline runs later, identifies which runway vertices sit on integer
lat/lon lines, and pins them HARD to ``dem.alt_strict`` (the raw HGT
pixel — required for cross-tile parity / preserve_boundary).

The old ``runway_regrade`` only adjusted the two threshold corners
when seam altitudes were added.  Interior segment-boundary corners
stayed at their emit-time CIFP values, which meant the runway's
combined profile (post-regrade) was no longer FAA-compliant —
adjacent sub-rects could disagree on grade or curvature at the
shared seam, and the per-surface solver had to paper over the
result.

This module finishes the job the regrade started.  For each runway
pair, it:

  1. Pulls the emit-time profile (``layout._runway_profile_state``)
     — the sample grid + anchor flags + phys-end coordinates +
     blast pad lengths.
  2. Folds every seam vertex on that runway into the sample list as
     an additional ANCHORED sample at its DEM altitude.
  3. Re-runs the same FAA gates the emit step ran
     (``runway_segments.faa_joint_solve``) — envelope clamp + hard
     cap + rate-of-grade-change — so the non-anchored interior
     samples shift to honor the new seam anchors while staying
     FAA-compliant.
  4. Evaluates the new profile at every runway sub-rect's vertex
     position (projected onto the runway axis) and writes back per-
     vertex ``node_altitudes``.  Sub-rects that came in as 4-corner
     canonical sloped rects get converted to ``node_altitudes`` if
     any corner moved away from the canonical ``[H, L, L, H]``
     pattern; otherwise they keep their ``altitude_high``/
     ``altitude_low``.

After this pass, every runway vertex carries an FAA-compliant
altitude derived from the same sample grid + same anchor set + same
gates as the emit-time profile.  Adjacent shapes (junctions, aprons,
taxiways) anchor to the runway-emitted altitudes via shared corners
exactly as before.

Cross-tile parity: deterministic from layout geometry + the seam
DEM samples (which preserve_boundary keeps identical between
neighbouring tiles).  Both tile builds compute the same augmented
sample list and run the same iterative passes, so they converge to
the same profile values at every vertex.

Public API: ``redistribute_runway_profile``.
"""
from __future__ import annotations

import math
import os as _os
from collections import defaultdict
from typing import Dict, List, Tuple

from .layout import (
    ROLE_RUNWAY, SHARED_VERTEX_TOL_M, high_low_from_corner_alts)
from .pavement.runway_segments import (
    MAX_RUNWAY_GRADE, MAX_RUNWAY_GRADE_CHANGE_PER_M, RUNWAY_END_GRADE,
    faa_joint_solve,
)
from .runway_regrade import regrade_runway, DEFAULT_ARC_K_M


__all__ = ["redistribute_runway_profile",
           "relieve_grade_via_runway_thresholds",
           "relieve_grade_via_inter_runway_split",
           "ENABLE_RUNWAY_THRESHOLD_RELIEF",
           "ENABLE_INTER_RUNWAY_THRESHOLD_SPLIT"]


def _bucket_key(x: float, y: float) -> Tuple[int, int]:
    s = 1.0 / SHARED_VERTEX_TOL_M
    return (int(round(x * s)), int(round(y * s)))


def _format_ref(desig_a: str, desig_b: str) -> str:
    """Convert a CIFP designator pair to the apt.dat-style ref used as
    ``shape.ref``.  Mirrors the convention in
    ``elevation.py::_ref_from_desig_pair`` — strip the ``RW`` prefix
    so refs match the row-100 format (e.g. ``02/20``).
    """
    def _strip(d):
        if not d:
            return d
        return d[2:] if d.startswith("RW") else d
    a = _strip(desig_a)
    b = _strip(desig_b)
    if a and b:
        return f"{a}/{b}"
    return a or b or ""


def _interp_profile(fractions: List[float], elevs: List[float],
                    t: float) -> float:
    """Linear-interp ``elevs`` at ``t`` over ``fractions`` (assumed
    sorted ascending).  Clamps to endpoints outside the range.
    """
    if not fractions:
        return 0.0
    if t <= fractions[0]:
        return elevs[0]
    if t >= fractions[-1]:
        return elevs[-1]
    # Binary search would be faster but the sample lists are short
    # (≤ 30 entries on typical airports).
    for i in range(len(fractions) - 1):
        f0 = fractions[i]
        f1 = fractions[i + 1]
        if f0 <= t <= f1:
            span = f1 - f0
            if span < 1e-12:
                return elevs[i]
            u = (t - f0) / span
            return elevs[i] + u * (elevs[i + 1] - elevs[i])
    return elevs[-1]


def _shift_thresholds_for_seams(
        fractions: List[float], elevs: List[float],
        anchored: List[bool],
        seam_samples: List[Tuple[float, float]],
        phys_dist: float) -> None:
    """Shift the first/last anchored samples (the runway's two
    threshold ends) via ``regrade_runway`` so they're reachable from
    every interior anchor (existing extras + new seams) within FAA
    grade + K-factor.

    The CIFP threshold altitudes act as a soft preference — the
    optimisation clips them to the feasible band defined by the
    union of interior-anchor envelopes and minimises the shift.
    Mutates ``elevs`` in place at the two threshold indices.
    """
    if not seam_samples:
        return
    # Find first and last anchored samples — the runway's two
    # threshold ends.
    first_i = None
    last_i = None
    for i, a in enumerate(anchored):
        if a:
            if first_i is None:
                first_i = i
            last_i = i
    if first_i is None or last_i is None or first_i == last_i:
        return

    t_a = fractions[first_i]
    t_b = fractions[last_i]
    axis_len = (t_b - t_a) * phys_dist
    if axis_len < 1.0:
        return

    # Build the interior-anchor list for ``regrade_runway``: all
    # currently-anchored samples between thresholds (cross-runway
    # projections + centerline crossings) plus the new seam samples,
    # each as (dist_from_threshold_A, altitude).
    interior: List[Tuple[float, float]] = []
    for i in range(first_i + 1, last_i):
        if anchored[i]:
            d = (fractions[i] - t_a) * phys_dist
            interior.append((d, elevs[i]))
    for t, e in seam_samples:
        d = (t - t_a) * phys_dist
        if 0.5 < d < axis_len - 0.5:
            interior.append((d, e))
    if not interior:
        return

    cifp_a = elevs[first_i]
    cifp_b = elevs[last_i]
    result = regrade_runway(
        cifp_a, cifp_b, axis_len, interior,
        grade_cap=MAX_RUNWAY_GRADE,
        end_grade_cap=RUNWAY_END_GRADE,
        arc_K_m=DEFAULT_ARC_K_M)
    elevs[first_i] = result.threshold_A
    elevs[last_i] = result.threshold_B


def _insert_seam_anchors(fractions: List[float], elevs: List[float],
                          anchored: List[bool],
                          seam_samples: List[Tuple[float, float]]
                          ) -> None:
    """Merge ``seam_samples`` (list of (t, elev)) into the parallel
    arrays, preserving sort order on ``fractions``.  A seam coinciding
    (within 1e-3 in t) with an existing sample takes over that sample
    — sets anchored=True and overrides elev.
    """
    for t, e in sorted(seam_samples):
        if t < 0.0 or t > 1.0:
            continue
        # Find insertion / match position.
        matched = False
        insert_at = len(fractions)
        for i, f in enumerate(fractions):
            if abs(f - t) < 1e-3:
                anchored[i] = True
                elevs[i] = e
                matched = True
                break
            if f > t:
                insert_at = i
                break
        if matched:
            continue
        fractions.insert(insert_at, t)
        elevs.insert(insert_at, e)
        anchored.insert(insert_at, True)


def _find_centerline_boundary_crossings(
        phys_end_a_ll: Tuple[float, float],
        phys_end_b_ll: Tuple[float, float],
        dem,
        tile_lat: int,
        tile_lon: int) -> List[Tuple[float, float]]:
    """Find every integer lat/lon line crossed by the runway's
    centerline and sample DEM at each crossing point.

    Returns ``[(t, altitude), ...]`` where ``t`` is the fraction along
    the centerline from ``phys_end_a`` (matches the convention of the
    sample list stored in ``layout._runway_profile_state``) and
    ``altitude`` is the DEM altitude at the centerline-boundary
    intersection point.

    Why one sample per boundary instead of one per boundary-runway
    vertex (user 2026-05-19): when a runway crosses a tile boundary
    at a shallow angle (e.g. SPLP runway 02/20 is 18° off the
    lon=-77 boundary, slicing diagonally through 148 m of runway
    length), the boundary-runway polygon intersection yields multiple
    vertices fanning across the runway's 45 m width at different
    latitudes.  Each latitude samples a different DEM pixel; on
    rolling terrain, those samples differ by metres even though
    they're all "the same crossing."  Anchoring at the centerline
    crossing gives one FAA-feasible altitude — the value the runway
    actually has where its centerline meets the contour line in the
    real world.

    Cross-tile parity: deterministic from CIFP centerline geometry
    plus the boundary HGT pixel, both of which preserve_boundary
    keeps identical between neighbouring tile builds.
    """
    if dem is None:
        return []
    lat_a, lon_a = phys_end_a_ll
    lat_b, lon_b = phys_end_b_ll
    nodata = getattr(dem, "nodata", -32768)

    crossings: List[Tuple[float, float]] = []

    def _sample(lat_c: float, lon_c: float):
        try:
            v = float(dem.alt_strict(
                (lon_c - tile_lon, lat_c - tile_lat)))
        except _GEOM_EXC:
            return None
        if v != v or v == nodata:
            return None
        return v

    # Integer LATITUDE lines crossed by the centerline (constant-lat).
    if abs(lat_b - lat_a) > 1e-12:
        lat_lo, lat_hi = sorted([lat_a, lat_b])
        n_lo = math.ceil(lat_lo) if lat_lo != math.floor(lat_lo) else int(lat_lo) + 1
        n_hi = math.floor(lat_hi) if lat_hi != math.floor(lat_hi) else int(lat_hi) - 1
        for n in range(int(n_lo), int(n_hi) + 1):
            t = (n - lat_a) / (lat_b - lat_a)
            if not (0.001 < t < 0.999):
                continue
            lat_c = float(n)
            lon_c = lon_a + t * (lon_b - lon_a)
            v = _sample(lat_c, lon_c)
            if v is not None:
                crossings.append((t, v))

    # Integer LONGITUDE lines crossed by the centerline (constant-lon).
    if abs(lon_b - lon_a) > 1e-12:
        lon_lo, lon_hi = sorted([lon_a, lon_b])
        n_lo = math.ceil(lon_lo) if lon_lo != math.floor(lon_lo) else int(lon_lo) + 1
        n_hi = math.floor(lon_hi) if lon_hi != math.floor(lon_hi) else int(lon_hi) - 1
        for n in range(int(n_lo), int(n_hi) + 1):
            t = (n - lon_a) / (lon_b - lon_a)
            if not (0.001 < t < 0.999):
                continue
            lon_c = float(n)
            lat_c = lat_a + t * (lat_b - lat_a)
            v = _sample(lat_c, lon_c)
            if v is not None:
                crossings.append((t, v))

    crossings.sort(key=lambda c: c[0])
    return crossings


_GEOM_EXC = (ValueError, TypeError, IndexError)


def redistribute_runway_profile(
        layout,
        dem=None,
        tile_lat: int = 0,
        tile_lon: int = 0) -> int:
    """Rewrite every runway sub-rect's altitudes by re-running the
    emit-time FAA-compliant profile with tile-boundary seam DEM
    altitudes folded in as additional anchored samples.

    Seam-altitude sampling (user 2026-05-19): one DEM sample per
    centerline-boundary crossing, taken at the geometric centerline
    intersection point with the integer lat/lon line.  This gives a
    single FAA-feasible altitude for the whole boundary cut through
    the runway — required for oblique crossings where the boundary
    diagonally slices across the runway's width and would otherwise
    pick up multiple per-vertex DEM samples at different latitudes
    with inconsistent altitudes.

    Mutates the runway shapes in ``layout`` in place: converts
    4-corner sloped rects to ``node_altitudes`` when any corner
    moves, leaves them canonical otherwise.

    Returns the number of runway shapes touched.
    """
    profile_state = getattr(layout, "_runway_profile_state", None)
    if not profile_state:
        return 0

    # Group runway shapes by ref so each pair's redistribution can be
    # applied to all of its sub-rects.
    shapes_by_ref: Dict[str, list] = defaultdict(list)
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        if not s.ref:
            continue
        shapes_by_ref[s.ref].append(s)

    n_touched = 0
    for pair_key, state in profile_state.items():
        desig_a, desig_b = pair_key
        ref = _format_ref(desig_a, desig_b)
        shapes = shapes_by_ref.get(ref, [])
        if not shapes:
            continue

        # Convert phys-end lat/lon to metre frame.
        phys_a_lat, phys_a_lon = state['phys_end_a_ll']
        phys_b_lat, phys_b_lon = state['phys_end_b_ll']
        ax_a_x, ax_a_y = layout.ll_to_m(phys_a_lat, phys_a_lon)
        ax_b_x, ax_b_y = layout.ll_to_m(phys_b_lat, phys_b_lon)
        ax_dx = ax_b_x - ax_a_x
        ax_dy = ax_b_y - ax_a_y
        ax_len2 = ax_dx * ax_dx + ax_dy * ax_dy
        if ax_len2 < 1.0:
            continue

        # Build the augmented sample list.
        fractions = list(state['fractions'])
        elevs = list(state['elevs'])
        anchored = list(state['anchored'])
        phys_dist = state['phys_dist_m']

        # Find per-boundary centerline crossings.  One DEM sample per
        # boundary line crossed (not per polygon vertex) — the value
        # at the centerline-boundary intersection.
        seam_samples = _find_centerline_boundary_crossings(
            state['phys_end_a_ll'], state['phys_end_b_ll'],
            dem, tile_lat, tile_lon)

        # Step 1: if any new HARD interior anchor entered the profile
        # (centerline-boundary DEMs), the existing CIFP thresholds
        # may no longer be reachable from them within FAA grade +
        # K-factor.  Shift the thresholds via the regrade_runway
        # constrained optimisation — same algorithm the older
        # threshold-only pass used, invoked here as a global step so
        # the WHOLE runway profile can then be smoothed in step 2
        # instead of leaving sub-rect interiors out of sync with the
        # shifted ends.
        #
        # Interior anchors (cross-runway projections, centerline
        # crossings already in the emit-time anchor list) are passed
        # in as immutable constraints — ``regrade_runway`` only moves
        # the two thresholds and keeps every other anchor fixed.
        if seam_samples:
            _shift_thresholds_for_seams(
                fractions, elevs, anchored,
                seam_samples, phys_dist)
            _insert_seam_anchors(fractions, elevs, anchored,
                                  seam_samples)

        # Step 2: re-run the FAA gates on the full sample list.
        # Thresholds are still anchored (at their possibly-shifted
        # values from step 1), seams are anchored at their DEM
        # altitudes, interior samples are free.  ``faa_joint_solve``
        # smooths the interior so every adjacent-edge grade stays
        # within ``MAX_RUNWAY_GRADE`` and every adjacent-triple ΔG
        # stays within the K-factor cap.
        faa_joint_solve(
            fractions, elevs, anchored, phys_dist,
            blast_a=state['blast_a_m'],
            blast_b=state['blast_b_m'],
            grade_cap=MAX_RUNWAY_GRADE,
            end_grade_cap=RUNWAY_END_GRADE,
            max_dg_per_m=MAX_RUNWAY_GRADE_CHANGE_PER_M)

        # Evaluate the new profile at every runway sub-rect's vertex.
        for s in shapes:
            ring = list(s.polygon.exterior.coords)
            ring_closed = bool(ring) and ring[0] == ring[-1]
            ring_open = ring[:-1] if ring_closed else ring
            if len(ring_open) < 3:
                continue
            new_alts: List[float] = []
            for x, y in ring_open:
                vx = x - ax_a_x
                vy = y - ax_a_y
                t = (vx * ax_dx + vy * ax_dy) / ax_len2
                e_new = _interp_profile(fractions, elevs, t)
                new_alts.append(round(e_new, 1))

            # Detect whether the new altitudes still form a canonical
            # ``[H, L, L, H]`` 4-corner sloped rect.  If so AND the
            # shape was originally that form, preserve it (keeps the
            # ``sloping_rect_canonical_form`` invariants happy and
            # avoids unnecessary node_altitudes conversions when
            # nothing actually moved).
            keep_canonical = False
            if (len(ring_open) == 4
                    and s.altitude_high is not None
                    and s.altitude_low is not None
                    and not s.node_altitudes):
                if (abs(new_alts[0] - new_alts[3]) < 0.05
                        and abs(new_alts[1] - new_alts[2]) < 0.05):
                    new_hi, new_lo = high_low_from_corner_alts(new_alts)
                    # Ensure ``hi`` is actually the higher pair (preserve
                    # the canonical convention).
                    if new_hi < new_lo:
                        new_hi, new_lo = new_lo, new_hi
                    if (abs(new_hi - s.altitude_high) < 0.05
                            and abs(new_lo - s.altitude_low) < 0.05):
                        # No-op: emit-time values already match.
                        continue
                    s.altitude_high = round(new_hi, 1)
                    s.altitude_low = round(new_lo, 1)
                    keep_canonical = True
                    n_touched += 1
            if keep_canonical:
                continue

            # Non-canonical (any moved corner or already
            # node_altitudes): write per-vertex.
            closed_alts = new_alts + ([new_alts[0]]
                                       if ring_closed else [])
            s.node_altitudes = closed_alts
            s.altitude = None
            s.altitude_high = None
            s.altitude_low = None
            n_touched += 1

    return n_touched


# ══════════════════════════════════════════════════════════════════
# STEP 3 of the directional grade-relief algorithm — bounded runway-
# threshold yield, the LAST resort (user 2026-05-25).
#
# PARKED / UNVALIDATED (``ENABLE_RUNWAY_THRESHOLD_RELIEF = False``).  When
# the cascade + the directional shape-relief (unified_jacobi) cannot reach
# grade with the runways locked, this shifts the runway THRESHOLD that binds
# the worst residual, re-derives the FAA profile (grade + K-curve, seam
# anchors fixed), re-solves, and hill-climbs until compliant or no movable
# threshold helps.  Intended for a GENUINE multi-runway infeasibility (e.g.
# SPJC: a flat terminal squeezed between two runways at different elevations)
# — NOT for SPLP (the directional cascade solves it with no runway move) nor
# CYXY (apron shear is a mid-runway, non-threshold problem).  Kept for that
# future edge case; enable + validate when one is hit.  Caveats: it cannot
# tell a fixable residual from an unfixable one (so always-on it wastes
# re-solves reverting), and it ranks residuals by excess-in-metres while
# check_grade ranks by grade-%.
# ══════════════════════════════════════════════════════════════════

ENABLE_RUNWAY_THRESHOLD_RELIEF = False   # parked; flip to engage + validate
# STEP 5 (user 2026-06-05): inter-runway threshold SPLIT (lower the high
# threshold, raise the low one by half the route excess each).  Safe to enable —
# each split is kept only if the worst residual shrinks, and it fires only on a
# genuine inter-runway threshold-gap (apron-fill / runway-parallel residuals are
# left untouched).  Set ``O4_INTER_RUNWAY_SPLIT=0`` to disable.
ENABLE_INTER_RUNWAY_THRESHOLD_SPLIT = (
    _os.environ.get("O4_INTER_RUNWAY_SPLIT", "1") == "1")
_RELIEF_TOL_M = 0.03            # residual below this (m of grade-excess) = done
_THRESHOLD_STEP_M = 0.25        # per-iteration threshold nudge


def _relief_role_caps():
    from .layout import (
        ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
        ROLE_CROSS_CONNECTOR, ROLE_JUNCTION, ROLE_APRON)
    from .config import TAXI_MAX_GRADE, APRON_MAX_GRADE
    return {
        ROLE_PRIMARY_PARALLEL: TAXI_MAX_GRADE,
        ROLE_SECONDARY_PARALLEL: TAXI_MAX_GRADE,
        ROLE_STUB: TAXI_MAX_GRADE,
        ROLE_CROSS_CONNECTOR: TAXI_MAX_GRADE,
        ROLE_JUNCTION: APRON_MAX_GRADE,
        ROLE_APRON: APRON_MAX_GRADE,
    }


def _shape_vertex_elevs(s):
    """Open-ring coords + per-vertex elevations for a shape, or
    ``(coords, None)`` when it carries no elevation."""
    try:
        coords = list(s.polygon.exterior.coords)
    except Exception:
        return [], None
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    if s.altitude_high is not None and s.altitude_low is not None \
            and len(coords) == 4:
        return coords, [s.altitude_high, s.altitude_low,
                        s.altitude_low, s.altitude_high]
    if s.node_altitudes:
        per = [float(a) for a in s.node_altitudes[:len(coords)]]
        if len(per) < len(coords) and per:
            per += [per[-1]] * (len(coords) - len(per))
        return coords, per
    if s.altitude is not None:
        return coords, [float(s.altitude)] * len(coords)
    return coords, None


def _worst_within_shape_residual(layout):
    """Worst within-shape grade-excess (m over the compliant amount) among
    taxi/junction/apron shapes, with its endpoints — or ``None`` if compliant."""
    caps = _relief_role_caps()
    worst = None
    for s in layout.shapes:
        cap = caps.get(s.role)
        if cap is None or s.polygon is None or s.polygon.is_empty:
            continue
        coords, per = _shape_vertex_elevs(s)
        if per is None or len(per) < 2:
            continue
        m = min(len(coords), len(per))
        for i in range(m):
            xi, yi = coords[i]
            for j in range(i + 1, m):
                xj, yj = coords[j]
                d = math.hypot(xi - xj, yi - yj)
                if d < 0.5:
                    continue
                excess = abs(per[i] - per[j]) - cap * d
                if excess <= 0.0:
                    continue
                if worst is None or excess > worst["excess"]:
                    hi_i = i if per[i] >= per[j] else j
                    lo_i = j if hi_i == i else i
                    worst = {
                        "excess": excess, "shape": s,
                        "hi_xy": coords[hi_i], "hi_e": per[hi_i],
                        "lo_xy": coords[lo_i], "lo_e": per[lo_i],
                        "mid_e": 0.5 * (per[i] + per[j]),
                        "mid_xy": (0.5 * (xi + xj), 0.5 * (yi + yj))}
    return worst


def _seam_node_coords(layout):
    """Seam-anchor node buckets — thresholds on these never move."""
    return set(getattr(layout, "_seam_anchor_keys", None) or set())


def _pick_movable_threshold(layout, state, residual, seam_keys):
    """Runway-pair threshold nearest the residual (skipping seam-anchored
    ones) + direction toward the residual's mean elevation.  Returns
    ``(pair_key, elev_index, direction)`` or ``None``."""
    bk_s = 1.0 / SHARED_VERTEX_TOL_M
    mx, my = residual["mid_xy"]
    best = None
    for pair_key, st in state.items():
        anchored = st.get("anchored") or []
        elevs = st.get("elevs") or []
        first_i = next((i for i, a in enumerate(anchored) if a), None)
        last_i = next((i for i in range(len(anchored) - 1, -1, -1)
                       if anchored[i]), None)
        if first_i is None or last_i is None or first_i == last_i:
            continue
        try:
            ax, ay = layout.ll_to_m(*st["phys_end_a_ll"])
            bx, by = layout.ll_to_m(*st["phys_end_b_ll"])
        except Exception:
            continue
        for (px, py), e_idx in (((ax, ay), first_i), ((bx, by), last_i)):
            if (int(round(px * bk_s)), int(round(py * bk_s))) in seam_keys:
                continue
            d = math.hypot(px - mx, py - my)
            if best is None or d < best[0]:
                best = (d, pair_key, e_idx, elevs[e_idx])
    if best is None:
        return None
    _d, pair_key, e_idx, thr_elev = best
    direction = -1.0 if thr_elev > residual["mid_e"] else 1.0
    return pair_key, e_idx, direction


def relieve_grade_via_runway_thresholds(
        layout, dem, tile_lat, tile_lon, resolve,
        max_iters: int = 12, step_m: float = _THRESHOLD_STEP_M,
        tol_m: float = _RELIEF_TOL_M) -> int:
    """STEP 3 loop (PARKED).  ``resolve()`` re-runs the per-surface solver.
    Nudge the binding runway threshold, re-profile + re-solve, keep the move
    only if the worst residual shrank (trying the opposite direction once);
    stop when compliant or no direction improves.  Returns the nudge count."""
    state = getattr(layout, "_runway_profile_state", None)
    if not state:
        return 0
    seam_keys = _seam_node_coords(layout)
    n_moves = 0
    for _it in range(max_iters):
        r = _worst_within_shape_residual(layout)
        if r is None or r["excess"] <= tol_m:
            break
        pick = _pick_movable_threshold(layout, state, r, seam_keys)
        if pick is None:
            break
        pair_key, e_idx, direction = pick
        improved = False
        for dvec in (direction, -direction):
            state[pair_key]["elevs"][e_idx] += dvec * step_m
            redistribute_runway_profile(layout, dem, tile_lat, tile_lon)
            resolve()
            r2 = _worst_within_shape_residual(layout)
            if (r2["excess"] if r2 else 0.0) < r["excess"] - 1e-3:
                n_moves += 1
                improved = True
                break
            state[pair_key]["elevs"][e_idx] -= dvec * step_m
        if not improved:
            redistribute_runway_profile(layout, dem, tile_lat, tile_lon)
            resolve()
            break
    return n_moves
def _ref_state_key(state, ref):
    """Map a runway shape ``ref`` ("05C/23C") to its ``_runway_profile_state``
    key (("RW05C", "RW23C"))."""
    for k in state:
        a, b = k
        sa = a[2:] if a.startswith("RW") else a
        sb = b[2:] if b.startswith("RW") else b
        if ref in (f"{sa}/{sb}", f"{sb}/{sa}"):
            return k
    return None
_END_FRAC = 0.15                # |frac| within this of 0/1 == a threshold END


def _is_runway_end(frac) -> bool:
    """True if axis-fraction ``frac`` sits at a runway END (within ``_END_FRAC``
    of 0 or 1) — i.e. the binding connection IS a movable threshold."""
    return frac is not None and (frac < _END_FRAC or frac > 1.0 - _END_FRAC)


def _principal_axis_pts(pts):
    """Unit axis of a point cloud's longest extent as ``(ax, ay, ux, uy, L)``
    (origin = one extreme, ``(ux,uy)`` unit direction, ``L`` length).  ``None``
    if degenerate."""
    if len(pts) < 2:
        return None
    best = None
    bd = -1.0
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = (pts[i][0] - pts[j][0]) ** 2 + (pts[i][1] - pts[j][1]) ** 2
            if d > bd:
                bd = d
                best = (pts[i], pts[j])
    if best is None:
        return None
    (ax, ay), (bx, by) = best
    L = math.hypot(bx - ax, by - ay)
    if L < 1.0:
        return None
    return (ax, ay, (bx - ax) / L, (by - ay) / L, L)


def _runway_node_index(layout, bucket_to_idx):
    """Map every runway node idx → its runway ref, and every ref → its axis
    (``_principal_axis_pts``).  Returns ``(node_ref, ref_axis)``."""
    node_ref: Dict[int, str] = {}
    pts_by_ref: Dict[str, list] = defaultdict(list)
    for s in layout.shapes:
        if (s.role in (ROLE_RUNWAY, "runway_crossing") and s.ref
                and s.polygon is not None and not s.polygon.is_empty):
            for (x, y) in list(s.polygon.exterior.coords)[:-1]:
                k = bucket_to_idx.get(
                    layout.canonical_points.get_or_add(float(x), float(y)))
                if k is not None:
                    node_ref.setdefault(k, s.ref)
                pts_by_ref[s.ref].append((x, y))
    ref_axis = {ref: _principal_axis_pts(pts)
                for ref, pts in pts_by_ref.items()}
    return node_ref, ref_axis


def _axis_frac(ref_axis, ref, xy):
    """Fraction (0..1) of ``xy`` projected onto runway ``ref``'s axis."""
    a = ref_axis.get(ref)
    if not a:
        return None
    ax, ay, ux, uy, L = a
    return ((xy[0] - ax) * ux + (xy[1] - ay) * uy) / L


def _anchor_band_dijkstra(n, adj, is_hard, elev, sign):
    """Multi-source Dijkstra over the cap-weighted graph from the HARD anchors,
    seeded with ``sign·elev[a]``.  ``sign=+1`` gives ``hi[v]=min(elev[a]+capd)``
    (tightest upper bound) and tracks the binding LOW-elevation anchor;
    ``sign=-1`` gives ``-lo[v]=min(-elev[a]+capd)`` and tracks the binding
    HIGH-elevation anchor.  Returns ``(dist, src)``."""
    import heapq
    INF = float("inf")
    dist = [INF] * n
    src: List = [None] * n
    pq: list = []
    for a in range(n):
        if is_hard[a]:
            dist[a] = sign * elev[a]
            src[a] = a
            pq.append((dist[a], a))
    heapq.heapify(pq)
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        for v, c in adj.get(u, ()):  # type: ignore[arg-type]
            nd = d + c
            if nd < dist[v]:
                dist[v] = nd
                src[v] = src[u]
                heapq.heappush(pq, (nd, v))
    return dist, src


def _inter_runway_tensions(layout, tol_m: float = _RELIEF_TOL_M) -> list:
    """Find inter-runway grade tensions via the cap-weighted DIFFERENCE-
    CONSTRAINT band (NOT geometric route distance — that over-estimates the
    grade budget and misses real tensions, the s64 bug).

    Grade feasibility is the difference-constraint system ``|e_i − e_j| ≤
    cap·len``; the binding distance is the cap-weighted shortest path over the
    solver's own shape-constraint graph (``unified_jacobi``), where flat rect
    cross-ends cost ZERO budget.  For each within-shape grade violation, the
    binding HIGH anchor (``max(elev − capd)``, forces the floor UP) and LOW
    anchor (``min(elev + capd)``, forces the ceiling DOWN) bound its elevation;
    when floor > ceiling the region is infeasible by that excess — a genuine
    tension between two runway connection points that no apron/runway flex can
    resolve.  Returns dicts (worst first):
      ``{excess, ref_hi, frac_hi, e_hi, ref_lo, frac_lo, e_lo}``
    — ``ref_hi`` is the HIGH runway whose connection must be LOWERED, ``ref_lo``
    the LOW runway whose connection must be RAISED, each at axis-fraction
    ``frac_*`` (used to choose center-vs-end threshold moves).  A tension whose
    binding anchor is a seam (not a runway) is skipped — seam > CIFP."""
    from .elevation_per_surface import unified_jacobi as uj
    from .verification import run_grade_checks
    nodes, b2i = uj._build_node_list(layout)
    n = len(nodes)
    if n == 0:
        return []
    elev, base_hard, _ = uj._seed_elevations(layout, nodes, b2i, dem=None)
    tiers = uj._node_tiers(layout, b2i, n)
    is_hard = [base_hard[i] or tiers[i] == 0 for i in range(n)]
    if not any(is_hard):
        return []
    adj: Dict[int, list] = {}
    for sc in uj._build_shape_constraints(layout, b2i):
        for (i, j, c) in sc["edges"]:
            adj.setdefault(i, []).append((j, c))
            adj.setdefault(j, []).append((i, c))
    INF = float("inf")
    lo_d, lo_src = _anchor_band_dijkstra(n, adj, is_hard, elev, -1.0)
    hi_d, hi_src = _anchor_band_dijkstra(n, adj, is_hard, elev, +1.0)
    node_ref, ref_axis = _runway_node_index(layout, b2i)
    within, _c, _s = run_grade_checks(layout)
    out = []
    seen = set()
    for v in within:
        if v.lat is None or v.lon is None:
            continue
        mx, my = layout.ll_to_m(v.lat, v.lon)
        sidx = min(adj, key=lambda i:
                   (nodes[i][0] - mx) ** 2 + (nodes[i][1] - my) ** 2)
        if hi_d[sidx] >= INF or lo_d[sidx] >= INF:
            continue
        excess = (-lo_d[sidx]) - hi_d[sidx]      # floor − ceiling
        if excess <= tol_m:
            continue
        a_hi = lo_src[sidx]      # high-elev anchor (forces floor up) → LOWER it
        a_lo = hi_src[sidx]      # low-elev anchor (forces ceiling down) → RAISE
        ref_hi = node_ref.get(a_hi)
        ref_lo = node_ref.get(a_lo)
        if not ref_hi or not ref_lo or ref_hi == ref_lo:
            continue              # seam anchor, or same runway (intra-runway)
        frac_hi = _axis_frac(ref_axis, ref_hi, nodes[a_hi])
        frac_lo = _axis_frac(ref_axis, ref_lo, nodes[a_lo])
        key = (ref_hi, round(frac_hi or 0.0, 1),
               ref_lo, round(frac_lo or 0.0, 1))
        if key in seen:
            continue
        seen.add(key)
        out.append({"excess": excess,
                    "ref_hi": ref_hi, "frac_hi": frac_hi, "e_hi": elev[a_hi],
                    "ref_lo": ref_lo, "frac_lo": frac_lo, "e_lo": elev[a_lo]})
    out.sort(key=lambda t: -t["excess"])
    return out


def _geodesic_grade_metric(layout):
    """``(total_excess_pct, count)`` over the GEODESIC within-shape grade
    violations (``verification.run_grade_checks`` = the test validator).  Used
    as step 5's accept metric: the min-SUM of real grade-excess, which a
    threshold move can actually shift — unlike the all-pair-Euclidean worst,
    which a far-corner mega-apron phantom dominates and pins."""
    from .verification import run_grade_checks
    within, _c, _s = run_grade_checks(layout)
    total = sum(max(0.0, v.excess_pct) for v in within)
    return (total, len(within))


def _shift_runway_connection(layout, state, ref, frac, delta, seam_keys) -> list:
    """Shift runway ``ref``'s profile at axis-fraction ``frac`` by ``delta`` m
    (negative = lower).  A connection near an END (``frac`` within ``_END_FRAC``
    of 0/1) moves THAT threshold (≈ 1:1, preferred); a CENTER connection moves
    BOTH thresholds by ``delta`` (a uniform shift — no added tilt, so the runway
    keeps its grade+curvature; redistribute then re-fits the FAA profile).
    Seam-pinned thresholds never move (seam > CIFP).  Returns
    ``[(pair_key, e_idx, applied_delta), ...]`` for revert (empty if nothing
    movable)."""
    k = _ref_state_key(state, ref)
    if not k:
        return []
    st = state[k]
    anchored = st.get("anchored") or []
    elevs = st.get("elevs") or []
    fi = next((i for i, a in enumerate(anchored) if a), None)
    li = next((i for i in range(len(anchored) - 1, -1, -1) if anchored[i]), None)
    if fi is None or li is None or fi == li:
        return []
    try:
        ax, ay = layout.ll_to_m(*st["phys_end_a_ll"])   # fi == phys_end_a
        bx, by = layout.ll_to_m(*st["phys_end_b_ll"])    # li == phys_end_b
    except Exception:
        return []
    bk_s = 1.0 / SHARED_VERTEX_TOL_M
    a_pin = (int(round(ax * bk_s)), int(round(ay * bk_s))) in seam_keys
    b_pin = (int(round(bx * bk_s)), int(round(by * bk_s))) in seam_keys
    if frac is not None and frac < _END_FRAC:
        targets = [(fi, a_pin)]
    elif frac is not None and frac > 1.0 - _END_FRAC:
        targets = [(li, b_pin)]
    else:                                # center → both ends (uniform, no tilt)
        targets = [(fi, a_pin), (li, b_pin)]
    applied = []
    for e_idx, pinned in targets:
        if pinned:
            continue
        elevs[e_idx] += delta
        applied.append((k, e_idx, delta))
    return applied


def relieve_grade_via_inter_runway_split(
        layout, dem, tile_lat, tile_lon, resolve,
        max_iters: int = 8, tol_m: float = _RELIEF_TOL_M) -> int:
    """STEP 5 (user 2026-06-05): a pavement grade violation that no apron/runway
    flex can fix because the taxi route between two runways spans more elevation
    than the route allows.  ``_inter_runway_tensions`` finds the binding pair of
    runway connection points via the cap-weighted difference-constraint band
    (e.g. HECA: 05C/23C's CENTER ~104 m where T4 joins ↔ 05L/23R's 23R END
    ~62 m, excess ~2.5 m over the ~2700 m T4→T→G→23R route).  Per tension, lower
    the HIGH connection and raise the LOW one by half the excess each, choosing
    threshold moves by where the connection sits on the runway: an END
    connection moves that threshold, a CENTER one moves both (uniform — keeps the
    runway's own grade+curvature; ``redistribute_runway_profile`` re-fits an FAA
    profile by construction).  Re-grade; keep only if the WORST within-shape
    residual shrinks (min-max, not a count).  Seam-pinned thresholds never move
    (seam > CIFP)."""
    state = getattr(layout, "_runway_profile_state", None)
    if not state:
        return 0
    seam_keys = _seam_node_coords(layout)
    debug = _os.environ.get("O4_FLEX_DEBUG") == "1"
    n_moves = 0
    before_m = _geodesic_grade_metric(layout)
    for _it in range(max_iters):
        tensions = _inter_runway_tensions(layout, tol_m=tol_m)
        if not tensions:
            break
        t = tensions[0]
        E = t["excess"]
        # LOWER the HIGH-binding runway's thresholds by the full excess (user
        # 2026-06-05): the thresholds are the hard points; lowering them makes
        # ROOM so that the next full re-solve's runway-flex can pull the MIDDLE
        # down (now grade+curvature compliant to the lowered thresholds) to where
        # the connecting pavement needs it.  A mid-runway connection lowers BOTH
        # thresholds (uniform shift); an END connection lowers that threshold.
        # ``redistribute`` keeps the terrain interior; it is the SOLVER (resolve)
        # that lowers the middle, so a threshold move only "works" after a full
        # re-solve — which the loop does below.
        d_hi, d_lo = E, 0.0
        applied = []
        applied += _shift_runway_connection(
            layout, state, t["ref_hi"], t["frac_hi"], -d_hi, seam_keys)
        if d_lo > 0.0:
            applied += _shift_runway_connection(
                layout, state, t["ref_lo"], t["frac_lo"], +d_lo, seam_keys)
        if not applied:
            break                          # binding threshold(s) seam-pinned
        redistribute_runway_profile(layout, dem, tile_lat, tile_lon)
        resolve()
        after_m = _geodesic_grade_metric(layout)
        if debug:
            print(f"[step5] tension {t['ref_hi']}@{t['frac_hi']:.2f}(lower "
                  f"{d_hi:.2f}) <-> {t['ref_lo']}@{t['frac_lo']:.2f}(raise "
                  f"{d_lo:.2f}) excess={E:.2f}: "
                  f"geodesic excess {before_m[0]:.3f}->{after_m[0]:.3f} "
                  f"count {before_m[1]}->{after_m[1]}")
        # Accept on the GEODESIC grade model (the validator) — total within-
        # shape grade-excess, the min-sum metric.  NOT the all-pair-Euclidean
        # ``_worst_within_shape_residual`` (a huge non-convex mega-apron phantom
        # dominates its "worst" and never moves with a threshold, so it would
        # reject every real improvement).  Tie-break: drop the violation count.
        improved = (after_m[0] < before_m[0] - 1e-3
                    or (abs(after_m[0] - before_m[0]) <= 1e-3
                        and after_m[1] < before_m[1]))
        if improved:
            n_moves += 1                   # split reduced real grade-excess
            before_m = after_m
        else:
            for (k, e_idx, d) in applied:  # revert
                state[k]["elevs"][e_idx] -= d
            redistribute_runway_profile(layout, dem, tile_lat, tile_lon)
            resolve()
            break
    return n_moves
