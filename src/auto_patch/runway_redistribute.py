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


def _build_pavement_graph(layout):
    """Shape-adjacency graph over the airside taxi/apron/runway network: a node
    per shape index, an edge (weighted by centroid distance) between shapes
    whose boundaries touch.  Returns ``(adj, runway_shapes_by_ref)`` or
    ``None``.  Routing on the CONNECTED network gives accurate taxi-route
    distances — no false straight-line connections between far-apart runway
    ends, and the real T4 route is found."""
    from shapely.strtree import STRtree
    NET = {"runway", "runway_crossing", "junction", "apron",
           "primary_parallel", "secondary_parallel", "stub", "cross_connector"}
    idxs = [i for i, s in enumerate(layout.shapes)
            if (s.role or "") in NET and s.polygon is not None
            and not s.polygon.is_empty]
    if len(idxs) < 2:
        return None
    geoms = [layout.shapes[i].polygon for i in idxs]
    cent = {idxs[k]: (g.centroid.x, g.centroid.y) for k, g in enumerate(geoms)}
    tree = STRtree(geoms)
    adj = {i: [] for i in idxs}
    for k, i in enumerate(idxs):
        g = geoms[k]
        for hk in tree.query(g.buffer(0.6)):
            hk = int(hk)
            if hk <= k:
                continue
            if g.distance(geoms[hk]) <= 0.6:           # boundaries touch
                j = idxs[hk]
                d = math.hypot(cent[i][0] - cent[j][0], cent[i][1] - cent[j][1])
                adj[i].append((j, d))
                adj[j].append((i, d))
    rwy_shapes = defaultdict(list)
    for i in idxs:
        s = layout.shapes[i]
        if (s.role or "") == "runway" and s.ref:
            rwy_shapes[s.ref].append(i)
    return adj, rwy_shapes


def _dijkstra_from(adj, srcs):
    """Multi-source Dijkstra.  Returns ``(dist, src_of)``: ``dist[n]`` = min
    route distance from any source to node ``n``; ``src_of[n]`` = the SOURCE
    node (the runway shape) the shortest path to ``n`` originates from — i.e.
    the runway EXIT the violation reaches, whose graded elevation (not the
    far threshold) is the binding runway level."""
    import heapq
    dist = {n: float("inf") for n in adj}
    src_of = {n: None for n in adj}
    pq = []
    for s in srcs:
        if s in dist:
            dist[s] = 0.0
            src_of[s] = s
            pq.append((0.0, s))
    heapq.heapify(pq)
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        for v, w in adj[u]:
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                src_of[v] = src_of[u]
                heapq.heappush(pq, (nd, v))
    return dist, src_of


def _runway_shape_elev(shape):
    """Mean graded elevation of a runway sub-rect (the connection level)."""
    if shape.altitude_high is not None and shape.altitude_low is not None:
        return 0.5 * (float(shape.altitude_high) + float(shape.altitude_low))
    if shape.node_altitudes:
        vals = [float(a) for a in shape.node_altitudes]
        return sum(vals) / len(vals) if vals else None
    if shape.altitude is not None:
        return float(shape.altitude)
    return None


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


def _nearest_movable_threshold(layout, state, ref, px, py, seam_keys):
    """Runway ``ref``'s threshold END nearest ``(px, py)`` as ``(pair_key,
    e_idx, elev)`` — the end the route connects to — or ``None`` if that end is
    seam-pinned (seam > CIFP) or unavailable."""
    k = _ref_state_key(state, ref)
    if not k:
        return None
    st = state[k]
    anchored = st.get("anchored") or []
    elevs = st.get("elevs") or []
    fi = next((i for i, a in enumerate(anchored) if a), None)
    li = next((i for i in range(len(anchored) - 1, -1, -1) if anchored[i]), None)
    if fi is None or li is None or fi == li:
        return None
    try:
        ax, ay = layout.ll_to_m(*st["phys_end_a_ll"])
        bx, by = layout.ll_to_m(*st["phys_end_b_ll"])
    except Exception:
        return None
    (ex, ey), e_idx = (((ax, ay), fi)
                       if math.hypot(ax - px, ay - py)
                       < math.hypot(bx - px, by - py) else ((bx, by), li))
    bk_s = 1.0 / SHARED_VERTEX_TOL_M
    if (int(round(ex * bk_s)), int(round(ey * bk_s))) in seam_keys:
        return None                                    # seam-pinned end
    return (k, e_idx, float(elevs[e_idx]))


def relieve_grade_via_inter_runway_split(
        layout, dem, tile_lat, tile_lon, resolve,
        max_iters: int = 8, tol_m: float = _RELIEF_TOL_M) -> int:
    """STEP 5 (user 2026-06-05): a pavement grade violation that no apron/runway
    flex can fix because the taxi route between two runways spans more elevation
    than the route allows.  Route each violation through the SHAPE-ADJACENCY
    network to every reachable runway, take its nearest threshold end on each,
    and form the violation's feasible elevation band ``[max(e_R − cap·d_R),
    min(e_R + cap·d_R)]``.  When that band is EMPTY the two binding runways are
    too far apart in elevation for their routes — SPLIT the gap: lower the
    high-binding threshold and raise the low-binding one by half each, RESET the
    runway geometry and re-grade (``redistribute_runway_profile`` re-fits an FAA
    compliant profile for the new thresholds, so the runway makes grade by
    construction).  HECA: the T4 region routes ~191 m to 23R (60 m) but ~3035 m
    to 23C (114 m) → lower 23C / raise 23R ~5 m each.  Seam-pinned thresholds
    never move (seam > CIFP).  Kept only if the worst residual shrinks."""
    from .config import TAXI_MAX_GRADE
    from .verification import run_grade_checks
    state = getattr(layout, "_runway_profile_state", None)
    if not state:
        return 0
    seam_keys = _seam_node_coords(layout)
    n_moves = 0
    for _it in range(max_iters):
        graph = _build_pavement_graph(layout)
        if graph is None:
            break
        adj, rwy_shapes = graph
        if len(rwy_shapes) < 2:
            break
        distmap = {ref: _dijkstra_from(adj, sh)
                   for ref, sh in rwy_shapes.items()}
        cent = {i: (layout.shapes[i].polygon.centroid.x,
                    layout.shapes[i].polygon.centroid.y) for i in adj}
        node_idxs = list(adj)
        # Within-shape grade violations from the GEODESIC visibility check (the
        # current grade model — NOT all-pair Euclidean).
        within, _cross, _steps = run_grade_checks(layout)
        if not within:
            break
        best = None
        for v in within:
            if v.lat is None or v.lon is None:
                continue
            mx, my = layout.ll_to_m(v.lat, v.lon)
            sidx = min(node_idxs, key=lambda i:
                       (cent[i][0] - mx) ** 2 + (cent[i][1] - my) ** 2)
            # ``hi`` = runway forcing the floor UP (lower its threshold);
            # ``lo`` = runway forcing the ceiling DOWN (raise its threshold).
            hi = lo = None                  # (state_key, e_idx, bound)
            for ref, (dist, src_of) in distmap.items():
                d = dist.get(sidx, float("inf"))
                if not (d < float("inf")):
                    continue                # this runway unreachable from here
                exit_idx = src_of.get(sidx)
                if exit_idx is None:
                    continue
                # Runway level at the EXIT the route reaches (NOT the far
                # threshold) — a mid-runway connection is at the dipped runway
                # level, not the end elevation.
                e_exit = _runway_shape_elev(layout.shapes[exit_idx])
                if e_exit is None:
                    continue
                ex, ey = cent[exit_idx]
                thr = _nearest_movable_threshold(
                    layout, state, ref, ex, ey, seam_keys)
                if thr is None:
                    continue                # the exit's nearest end is seam-pinned
                k, e_idx, _e_thr = thr
                lower = e_exit - TAXI_MAX_GRADE * d
                upper = e_exit + TAXI_MAX_GRADE * d
                if hi is None or lower > hi[2]:
                    hi = (k, e_idx, lower)
                if lo is None or upper < lo[2]:
                    lo = (k, e_idx, upper)
            if hi is None or lo is None or hi[0] == lo[0]:
                continue
            gap = hi[2] - lo[2]             # max_lower − min_upper
            if gap > tol_m and (best is None or gap > best[0]):
                best = (gap, hi, lo)
        if best is None:
            break                          # every violation's band is feasible
        gap, hi, lo = best
        split = gap / 2.0
        before_n = len(within)
        state[hi[0]]["elevs"][hi[1]] -= split     # lower the high binding end
        state[lo[0]]["elevs"][lo[1]] += split     # raise the low binding end
        redistribute_runway_profile(layout, dem, tile_lat, tile_lon)
        resolve()
        after_n = len(run_grade_checks(layout)[0])
        if _os.environ.get("O4_FLEX_DEBUG") == "1":
            print(f"[step5] gap={gap:.2f} lo {lo[0]}+{split:.2f} / "
                  f"hi {hi[0]}-{split:.2f}: geodesic viol {before_n}->{after_n}")
        if after_n < before_n:
            n_moves += 1                   # split cleared a violation — keep it
        else:
            state[hi[0]]["elevs"][hi[1]] += split   # revert
            state[lo[0]]["elevs"][lo[1]] -= split
            redistribute_runway_profile(layout, dem, tile_lat, tile_lon)
            resolve()
            break
    return n_moves
