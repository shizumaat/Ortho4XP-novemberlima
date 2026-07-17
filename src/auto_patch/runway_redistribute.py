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
lat/lon lines, and pins them HARD to the SMOOTHED DEM value there
(``elevation._sample_dem`` — the seam ruling; cross-tile parity holds
because preserve_boundary makes neighbouring tiles agree at the line).

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
SMOOTHED-DEM samples (which preserve_boundary keeps identical between
neighbouring tiles).  Both tile builds compute the same augmented
sample list and run the same iterative passes, so they converge to
the same profile values at every vertex.

Public API: ``redistribute_runway_profile``.
"""
from __future__ import annotations

import math
import os
from collections import defaultdict
from typing import Dict, List, Tuple

from .config import RUNWAY_END_FRACTION, RUNWAY_THRESHOLD_STRICT_M
from .layout import ROLE_RUNWAY, SHARED_VERTEX_TOL_M
from .pavement.runway_segments import (
    MAX_RUNWAY_GRADE, MAX_RUNWAY_GRADE_CHANGE_PER_M, RUNWAY_END_GRADE,
    faa_joint_solve,
)
from .runway_regrade import regrade_runway, DEFAULT_ARC_K_M


__all__ = ["redistribute_runway_profile", "apply_runway_flex",
           "flex_slack_at", "solve_profile_with_minimal_end_zone_cap"]


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


def sample_redistributed_profile(layout, ref: str,
                                 x: float, y: float) -> "float | None":
    """Elevation of runway ``ref``'s redistributed FAA profile at
    metre-frame point ``(x, y)`` — projection onto the physical-end
    axis, linear interpolation over the gated sample list.

    This is the runway's AUTHORITATIVE elevation surface (CIFP
    thresholds + seam DEM anchors, smoothed through the FAA grade and
    K-factor gates), laterally flat by construction.  Deterministic
    across adjacent tile builds: both tiles derive it from the same
    CIFP geometry and the same boundary HGT pixels.  Returns ``None``
    when ``redistribute_runway_profile`` has not stored a profile for
    ``ref`` (no CIFP state).
    """
    profiles = getattr(layout, "_runway_redistributed_profiles", None)
    if not profiles:
        return None
    p = profiles.get(ref)
    if not p:
        return None
    ax_x, ax_y = p['axis_a']
    dx, dy = p['axis_d']
    t = ((x - ax_x) * dx + (y - ax_y) * dy) / p['axis_len2']
    return _interp_profile(p['fractions'], p['elevs'], t)


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

    Cross-tile parity: deterministic from CIFP centerline geometry plus
    the SMOOTHED DEM at the boundary line (``preserve_boundary`` blends
    the smoothing toward the shared edge row, so neighbouring tile
    builds interpolate the same values there).

    USER RULING (2026-06-28, re-affirmed 2026-07-06): seam values sample
    the SMOOTHED DEM through the same interpolating sampler the rest of
    the build uses (``elevation._sample_dem`` → ``dem.alt``) — never
    ``dem.alt_strict`` (nearest-pixel; nodata exactly ON the tile edge).
    """
    if dem is None:
        return []
    lat_a, lon_a = phys_end_a_ll
    lat_b, lon_b = phys_end_b_ll
    nodata = getattr(dem, "nodata", -32768)

    crossings: List[Tuple[float, float]] = []

    def _sample(lat_c: float, lon_c: float):
        from .elevation import _sample_dem
        try:
            v = _sample_dem(dem, tile_lat, tile_lon, lat_c, lon_c)
        except _GEOM_EXC:
            return None
        if v is None or v != v or v == nodata:
            return None
        return float(v)

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


def _find_edge_boundary_crossings(
        layout,
        runway_shapes,
        phys_end_a_ll: Tuple[float, float],
        phys_end_b_ll: Tuple[float, float],
        dem,
        tile_lat: int,
        tile_lon: int) -> List[Tuple[float, float]]:
    """Sample DEM where the runway EDGES cross each integer lat/lon
    line — two anchors per boundary crossing instead of the single
    centerline sample (user 2026-07-04, SPLP west edge).

    The tile cut leaves a ~10 m unpaved strip at the boundary whose
    mesh spans the two patch edges — so the runway's VISIBLE terrain
    contacts at a seam are the points where its edges meet the line,
    not the centerline midpoint.  At SPLP's 18° oblique crossing the
    edges meet the line ~141 m of station apart, and the single
    centerline anchor left the west edge 2 m under the local terrain
    (profile 58.5 vs DEM 60.6 — the reported dip).  Anchoring the
    profile at each edge crossing's own DEM (the stations differ, so
    the profile can hold both within grade) puts the pavement exactly
    on the terrain at both visible contacts.

    Returns ``[(t, altitude), ...]`` like the centerline variant;
    empty when geometry/DEM makes no crossing usable (caller falls
    back to the centerline samples).
    """
    if dem is None or not runway_shapes:
        return []
    from shapely.geometry import LineString as _LS
    from shapely.ops import unary_union as _uu
    try:
        union = _uu([s.polygon for s in runway_shapes])
        min_x, min_y, max_x, max_y = union.bounds
    except _GEOM_EXC:
        return []

    lat_a, lon_a = phys_end_a_ll
    lat_b, lon_b = phys_end_b_ll
    ax_a_x, ax_a_y = layout.ll_to_m(lat_a, lon_a)
    ax_b_x, ax_b_y = layout.ll_to_m(lat_b, lon_b)
    ax_dx, ax_dy = ax_b_x - ax_a_x, ax_b_y - ax_a_y
    ax_len2 = ax_dx * ax_dx + ax_dy * ax_dy
    if ax_len2 < 1.0:
        return []
    nodata = getattr(dem, "nodata", -32768)

    # SMOOTHED-DEM sampler per the seam ruling (2026-06-28 / 2026-07-06)
    # — see _find_centerline_boundary_crossings.
    def _sample(lat_c: float, lon_c: float):
        from .elevation import _sample_dem
        try:
            v = _sample_dem(dem, tile_lat, tile_lon, lat_c, lon_c)
        except _GEOM_EXC:
            return None
        if v is None or v != v or v == nodata:
            return None
        return float(v)

    # Integer lat/lon lines in LOCAL METERS across the runway bbox.
    lines = []
    lat_lo, lon_lo = layout.m_to_ll(min_x, min_y)
    lat_hi, lon_hi = layout.m_to_ll(max_x, max_y)
    for n in range(int(math.ceil(min(lat_lo, lat_hi))),
                   int(math.floor(max(lat_lo, lat_hi))) + 1):
        p0 = layout.ll_to_m(float(n), min(lon_lo, lon_hi) - 1e-3)
        p1 = layout.ll_to_m(float(n), max(lon_lo, lon_hi) + 1e-3)
        lines.append(_LS([p0, p1]))
    for n in range(int(math.ceil(min(lon_lo, lon_hi))),
                   int(math.floor(max(lon_lo, lon_hi))) + 1):
        p0 = layout.ll_to_m(min(lat_lo, lat_hi) - 1e-3, float(n))
        p1 = layout.ll_to_m(max(lat_lo, lat_hi) + 1e-3, float(n))
        lines.append(_LS([p0, p1]))

    crossings: List[Tuple[float, float]] = []
    for line in lines:
        try:
            inter = union.intersection(line)
        except _GEOM_EXC:
            continue
        if inter.is_empty:
            continue
        pts = []
        parts = ([inter] if inter.geom_type == "LineString"
                 else list(getattr(inter, "geoms", ())))
        for part in parts:
            coords = list(getattr(part, "coords", ()))
            if coords:
                pts.extend([coords[0], coords[-1]])
        if len(pts) < 2:
            continue
        # The OUTER edge contacts = the two extreme points along the line.
        pts.sort(key=lambda c: (c[0], c[1]))
        for (ex, ey) in (pts[0], pts[-1]):
            t = ((ex - ax_a_x) * ax_dx + (ey - ax_a_y) * ax_dy) / ax_len2
            if not (0.001 < t < 0.999):
                continue
            lat_c, lon_c = layout.m_to_ll(ex, ey)
            v = _sample(lat_c, lon_c)
            if v is not None:
                crossings.append((t, v))
    crossings.sort(key=lambda c: c[0])
    return crossings


_GEOM_EXC = (ValueError, TypeError, IndexError)


def _worst_segment_over_main_cap(fractions: List[float],
                                 elevs: List[float],
                                 axis_length_m: float,
                                 grade_cap: float = MAX_RUNWAY_GRADE):
    """Worst consecutive-sample segment whose grade exceeds the MAIN
    longitudinal cap, as ``(excess, midpoint_t)`` — or ``None`` when the
    whole profile is law-compliant.  Shared by the minimal end-zone-cap
    escalation and the flex VERIFY-AND-RELAX loop (same tolerance)."""
    worst = None
    for k in range(1, len(fractions)):
        run = (fractions[k] - fractions[k - 1]) * axis_length_m
        if run < 0.5:
            continue
        grade = abs(elevs[k] - elevs[k - 1]) / run
        excess = grade - grade_cap - 1e-4
        if excess > 0 and (worst is None or excess > worst[0]):
            midpoint = 0.5 * (fractions[k] + fractions[k - 1])
            worst = (excess, midpoint)
    return worst


def _strict_budget_between(s_lo: float, s_hi: float, phys_dist: float) -> float:
    """Max grade-compliant height change between two stations (metres from
    threshold A) under the STRICT end-zone preference: 0.8% within the
    first/last ``RUNWAY_END_FRACTION`` of the length, 1.5% in the interior.
    Integrates the position-dependent cap over [s_lo, s_hi]."""
    end_len = RUNWAY_END_FRACTION * phys_dist
    lo_b, hi_b = end_len, phys_dist - end_len   # cap breakpoints
    budget = 0.0
    cursor = s_lo
    for edge, cap in ((lo_b, RUNWAY_END_GRADE),
                      (hi_b, MAX_RUNWAY_GRADE),
                      (phys_dist, RUNWAY_END_GRADE)):
        if cursor >= s_hi:
            break
        seg_hi = min(edge, s_hi)
        if seg_hi > cursor:
            budget += (seg_hi - cursor) * cap
            cursor = seg_hi
    return budget


def _end_zone_binding_report(fractions: List[float], elevs: List[float],
                             anchored: List[bool], phys_dist: float
                             ) -> List[str]:
    """INSTRUMENT-FIRST (user 2026-07-16, KBNA 13/31 defect G): explain
    WHY the strict 0.8% end-zone preference is infeasible — which HARD
    anchors bind and by how much.

    Under the strict preference every end-zone segment is capped at
    ``RUNWAY_END_GRADE`` (0.8%) and the interior at ``MAX_RUNWAY_GRADE``
    (1.5%).  For every pair of hard anchors (the CIFP thresholds + tile
    seam / crossing pins — none of which move), the maximum height a
    grade-compliant profile can span between them is the strict tiered
    budget over their station separation.  Any pair whose required rise
    exceeds that budget is a binding constraint; they are reported worst
    first.  Anchor-derived, so it is the true cause independent of the
    solve's projection order."""
    hard = [i for i, a in enumerate(anchored) if a]
    if len(hard) < 2:
        return []
    end_frac = RUNWAY_END_FRACTION
    binders = []
    for a in range(len(hard)):
        for b in range(a + 1, len(hard)):
            i, j = hard[a], hard[b]
            s_i = fractions[i] * phys_dist
            s_j = fractions[j] * phys_dist
            s_lo, s_hi = min(s_i, s_j), max(s_i, s_j)
            dist_m = s_hi - s_lo
            if dist_m < 0.5:
                continue
            required = abs(elevs[i] - elevs[j])
            budget = _strict_budget_between(s_lo, s_hi, phys_dist)
            deficit = required - budget
            if deficit > 1e-3:
                binders.append((deficit, i, j, s_lo, s_hi, dist_m, required,
                                budget))
    binders.sort(reverse=True)
    lines: List[str] = []
    for (deficit, i, j, s_lo, s_hi, dist_m, required, budget) in binders[:4]:
        avg = required / dist_m if dist_m > 0 else 0.0
        which = []
        if s_lo < end_frac * phys_dist:
            which.append("A")
        if s_hi > (1.0 - end_frac) * phys_dist:
            which.append("B")
        end_note = (f" (binds end {'+'.join(which)})" if which else "")
        lines.append(
            f"hard anchors {elevs[i]:.2f} m @ {s_lo:.0f} m and "
            f"{elevs[j]:.2f} m @ {s_hi:.0f} m: need {required:.2f} m over "
            f"{dist_m:.0f} m (avg {avg * 100:.2f}%) but the strict 0.8%/1.5% "
            f"tiered budget allows only {budget:.2f} m — deficit "
            f"{deficit:.2f} m{end_note}.")
    return lines


def solve_profile_with_minimal_end_zone_cap(
        fractions: List[float], elevs: List[float],
        anchored: List[bool], phys_dist: float, *,
        blast_a: float = 0.0, blast_b: float = 0.0,
        threshold_strict_m: float = 0.0,
        report: "dict | None" = None) -> float:
    """Run ``faa_joint_solve`` with the end-zone cap escalated MINIMALLY.

    RELAXATION ORDER (user ruling 2026-07-08): the main longitudinal
    grade cap (``MAX_RUNWAY_GRADE``, 1.5%) is LAW; the end-zone cap
    (``RUNWAY_END_GRADE``, 0.8%, EASA/ICAO first/last-quarter comfort
    rule) is a solver-internal PREFERENCE.  When hard anchors (CIFP
    thresholds, tile-seam DEM pins) make both unsatisfiable, the
    preference yields — and only by the minimum: the end-zone cap
    escalates to the smallest value in (0.8%, 1.5%] whose solve leaves
    no segment over the main cap.  Runways feasible at 0.8% keep 0.8%
    verbatim (per-runway escalation only).

    Without this, ``faa_hard_cap_pass`` midpoints the samples inside
    the infeasible end-zone band and spills the anchor deficit into
    the mid-runway as >1.5% segments (SPLP 02/20: CIFP threshold A
    49.00 m at station 251 m → seam DEM pin 60.47 m at station 1023 m
    needs +11.47 m, but the 0.8%-capped first quarter allows only
    +8.68 m; the 2.78 m deficit emitted as a 1.52–1.97% mid-runway
    ramp — a runway-LAW violation traded for the preference).

    TIERED relaxation (user 2026-07-16, KBNA 13/31 defect G): when
    ``threshold_strict_m`` > 0, the escalation is split into two bands.
    The last ``threshold_strict_m`` before EACH threshold holds the
    strict 0.8% cap; only the OUTER part of the end zone (from there to
    ``RUNWAY_END_FRACTION``) escalates — so the immediate threshold
    vicinity stays gentle while the deficit is absorbed deeper in the
    end zone.  The threshold band relaxes only when the profile is
    genuinely infeasible even with the whole outer end zone at the 1.5%
    law; that case escalates the threshold band minimally and is
    reported as a loud WARN by the caller.

    Escalation search: bisection on the escalating cap over
    (RUNWAY_END_GRADE, MAX_RUNWAY_GRADE], to 0.01%-grade granularity
    (1e-4 absolute), each attempt restarted from the SAME pre-solve
    sample values (the joint solve is a mutating projection; its result
    is path-dependent, so every attempt must start from identical
    state).  If even the uniform main cap cannot satisfy the anchors,
    the main-cap solve is kept as-was (least-bad; matches the historical
    uniform-cap behaviour — the validator is the backstop).

    Mutates ``elevs`` in place with the accepted solve.  Returns the
    (outer) end-zone cap the accepted solve used.  When ``report`` is a
    dict it is filled with ``end_zone_cap`` / ``threshold_cap`` /
    ``threshold_strict_fraction`` / ``binding`` (the instrument-first
    reason list, non-empty only when the 0.8% preference is infeasible).
    """
    initial_elevs = list(elevs)
    tsf = 0.0
    if threshold_strict_m > 0.0 and phys_dist > 0.0:
        tsf = min(threshold_strict_m / phys_dist, RUNWAY_END_FRACTION)
    tiered = tsf > 0.0

    def _attempt(end_zone_cap: float, threshold_cap: "float | None"):
        candidate = list(initial_elevs)
        faa_joint_solve(
            fractions, candidate, anchored, phys_dist,
            blast_a=blast_a, blast_b=blast_b,
            grade_cap=MAX_RUNWAY_GRADE,
            end_grade_cap=end_zone_cap,
            max_dg_per_m=MAX_RUNWAY_GRADE_CHANGE_PER_M,
            threshold_strict_cap=threshold_cap,
            threshold_strict_fraction=tsf)
        compliant = _worst_segment_over_main_cap(
            fractions, candidate, phys_dist) is None
        return candidate, compliant

    def _record(end_cap: float, thr_cap: "float | None",
                binding: "list | None" = None):
        if report is not None:
            report['end_zone_cap'] = end_cap
            report['threshold_cap'] = (thr_cap if thr_cap is not None
                                       else end_cap)
            report['threshold_strict_fraction'] = tsf
            if binding is not None:
                report['binding'] = binding

    # The strict band cap the escalation holds fixed (None when not tiered
    # so the grade-cap machinery keeps the historical single-end-zone-cap
    # behaviour verbatim).
    strict = RUNWAY_END_GRADE if tiered else None

    # 1. The preference first: keep 0.8% verbatim whenever feasible.
    candidate, compliant = _attempt(RUNWAY_END_GRADE, strict)
    if compliant:
        elevs[:] = candidate
        _record(RUNWAY_END_GRADE, RUNWAY_END_GRADE, [])
        return RUNWAY_END_GRADE

    # Preference infeasible — record WHY (which anchors bind).
    binding = _end_zone_binding_report(fractions, initial_elevs,
                                       anchored, phys_dist)

    # 2. Escalate the OUTER end zone only, threshold band held strict.
    infeasible_cap = RUNWAY_END_GRADE
    accepted, compliant = _attempt(MAX_RUNWAY_GRADE, strict)
    if compliant:
        accepted_cap = MAX_RUNWAY_GRADE
        while accepted_cap - infeasible_cap > 1e-4:
            midpoint_cap = 0.5 * (accepted_cap + infeasible_cap)
            cand, ok = _attempt(midpoint_cap, strict)
            if ok:
                accepted_cap, accepted = midpoint_cap, cand
            else:
                infeasible_cap = midpoint_cap
        elevs[:] = accepted
        _record(accepted_cap, RUNWAY_END_GRADE if tiered else accepted_cap,
                binding)
        return accepted_cap

    # 3. Genuinely infeasible even with the outer end zone at the 1.5%
    #    law and the threshold band strict.  When tiered, relax the
    #    threshold band too — minimally — so the deficit resolves; the
    #    caller WARNs loudly with the achieved threshold-band cap.
    if tiered:
        uniform_solve, uniform_ok = _attempt(MAX_RUNWAY_GRADE,
                                              MAX_RUNWAY_GRADE)
        if uniform_ok:
            thr_infeasible = RUNWAY_END_GRADE
            thr_cap, acc = MAX_RUNWAY_GRADE, uniform_solve
            while thr_cap - thr_infeasible > 1e-4:
                mid = 0.5 * (thr_cap + thr_infeasible)
                cand, ok = _attempt(MAX_RUNWAY_GRADE, mid)
                if ok:
                    thr_cap, acc = mid, cand
                else:
                    thr_infeasible = mid
            elevs[:] = acc
            _record(MAX_RUNWAY_GRADE, thr_cap, binding)
            return MAX_RUNWAY_GRADE
        # Infeasible even at the uniform law — keep it (least-bad).
        elevs[:] = uniform_solve
        _record(MAX_RUNWAY_GRADE, MAX_RUNWAY_GRADE, binding)
        return MAX_RUNWAY_GRADE

    # tsf == 0 (legacy): anchors infeasible even at the uniform LAW cap —
    # keep the main-cap solve.
    elevs[:] = accepted
    _record(MAX_RUNWAY_GRADE, None, binding)
    return MAX_RUNWAY_GRADE


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

        # Anchor at the runway EDGE crossings of each boundary line
        # (two DEM samples per crossing — the runway's VISIBLE terrain
        # contacts at the seam strip; user 2026-07-04, SPLP west edge:
        # the tile line renders from the smoothed DEM (preserve_boundary),
        # and the profile sat 2.5 m UNDER it — a terrain hump across the
        # runway).  Only crossings where the terrain line pokes ABOVE the
        # current profile anchor (the hump class): anchoring the ravine
        # side too dragged the neighbouring interior samples down ~2 m
        # (measured: the taxiway stub's reach ceiling fell 0.8 m).  Fall
        # back to the single centerline crossing when the edge walk
        # yields nothing (or the gate is off).
        seam_samples = []
        if os.environ.get("O4_RUNWAY_SEAM_EDGE_ANCHORS", "1") == "1":
            edge_samples = _find_edge_boundary_crossings(
                layout, shapes,
                state['phys_end_a_ll'], state['phys_end_b_ll'],
                dem, tile_lat, tile_lon)
            seam_samples = [
                (t, v) for (t, v) in edge_samples
                if v > _interp_profile(state['fractions'],
                                       state['elevs'], t) + 0.05]
        if not seam_samples:
            seam_samples = _find_centerline_boundary_crossings(
                state['phys_end_a_ll'], state['phys_end_b_ll'],
                dem, tile_lat, tile_lon)
        if os.environ.get("O4_SEAM_DEBUG") == "1":
            _es = _find_edge_boundary_crossings(
                layout, shapes,
                state['phys_end_a_ll'], state['phys_end_b_ll'],
                dem, tile_lat, tile_lon)
            print(f"    [seam-debug] tile=({tile_lat},{tile_lon}) "
                  f"edge_samples={[(round(t,4), round(v,2)) for t, v in _es]}")
            print(f"    [seam-debug] pre-shift profile at those t: "
                  f"{[(round(t,4), round(_interp_profile(state['fractions'], state['elevs'], t), 2)) for t, _v in _es]}")
            print(f"    [seam-debug] kept seam_samples="
                  f"{[(round(t,4), round(v,2)) for t, v in seam_samples]}")

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
        # altitudes, interior samples are free.  The joint solve
        # smooths the interior so every adjacent-edge grade stays
        # within ``MAX_RUNWAY_GRADE`` and every adjacent-triple ΔG
        # stays within the K-factor cap.  The end-zone cap (0.8%
        # preference) escalates MINIMALLY when the hard anchors make
        # it unsatisfiable alongside the main-cap LAW — see
        # ``solve_profile_with_minimal_end_zone_cap``.
        end_zone_report: dict = {}
        # TIERED end-zone relaxation (defect G) — O4_RUNWAY_TIERED_END=0
        # reverts to the historical single-end-zone-cap escalation.
        _strict_m = (RUNWAY_THRESHOLD_STRICT_M
                     if os.environ.get("O4_RUNWAY_TIERED_END", "1") == "1"
                     else 0.0)
        end_zone_cap = solve_profile_with_minimal_end_zone_cap(
            fractions, elevs, anchored, phys_dist,
            blast_a=state['blast_a_m'],
            blast_b=state['blast_b_m'],
            threshold_strict_m=_strict_m,
            report=end_zone_report)
        threshold_cap = end_zone_report.get('threshold_cap', end_zone_cap)
        threshold_strict_fraction = end_zone_report.get(
            'threshold_strict_fraction', 0.0)
        binding_lines = end_zone_report.get('binding') or []
        escalated = end_zone_cap > RUNWAY_END_GRADE + 1e-9
        threshold_relaxed = threshold_cap > RUNWAY_END_GRADE + 1e-9
        if escalated or threshold_relaxed:
            try:
                from O4_UI_Utils import vprint
                if threshold_relaxed:
                    # The last ~90 m before a threshold could NOT hold the
                    # gentle 0.8% cap — the genuinely-infeasible case.  Loud.
                    vprint(1, f"  [pav-builder] runway {ref}: WARNING — the "
                              f"threshold vicinity ({RUNWAY_THRESHOLD_STRICT_M:.0f} m) "
                              f"could not hold the strict "
                              f"{RUNWAY_END_GRADE * 100:.1f}% cap even with "
                              f"the outer end zone at the "
                              f"{MAX_RUNWAY_GRADE * 100:.1f}% law — threshold "
                              f"band escalated to {threshold_cap * 100:.2f}%.")
                else:
                    vprint(1, f"  [pav-builder] runway {ref}: end-zone "
                              f"grade preference "
                              f"{RUNWAY_END_GRADE * 100:.1f}% infeasible "
                              f"with hard anchors — outer end zone escalated "
                              f"to {end_zone_cap * 100:.2f}%; the last "
                              f"{RUNWAY_THRESHOLD_STRICT_M:.0f} m before each "
                              f"threshold held at "
                              f"{RUNWAY_END_GRADE * 100:.1f}% (main "
                              f"{MAX_RUNWAY_GRADE * 100:.1f}% cap is law).")
                for _line in binding_lines:
                    vprint(1, f"  [pav-builder] runway {ref}: {_line}")
            except ImportError:
                pass
        if os.environ.get("O4_END_ZONE_DEBUG") == "1":
            print(f"    [end-zone] runway {ref}: outer_cap="
                  f"{end_zone_cap * 100:.2f}% threshold_cap="
                  f"{threshold_cap * 100:.2f}% strict_frac="
                  f"{threshold_strict_fraction:.4f}")
            for _line in binding_lines:
                print(f"    [end-zone] runway {ref}: {_line}")

        # Persist the gated profile so later passes can evaluate the
        # runway's authoritative elevation at any point (``tile_cut``
        # rewrites cut-piece vertices from it — per-vertex DEM pins at
        # an oblique seam crossing fan across the runway's width and
        # carve terrain notches into the FAA profile; see
        # ``sample_redistributed_profile``).
        profiles = getattr(layout, "_runway_redistributed_profiles", None)
        if profiles is None:
            profiles = {}
            layout._runway_redistributed_profiles = profiles
        # Half-width for the clamp floor's cross-section distance
        # (``seam_anchors.runway_clamp_floor``): max lateral offset of any
        # runway ring vertex from the axis.  Computed BEFORE the tile cut
        # from the whole-runway sub-rects, so both tile builds persist the
        # same value (cross-tile determinism).
        axis_len = math.sqrt(ax_len2)
        unit_x, unit_y = ax_dx / axis_len, ax_dy / axis_len
        half_width = 0.0
        for s in shapes:
            for (vx, vy) in s.polygon.exterior.coords:
                lateral = abs(-(vx - ax_a_x) * unit_y
                              + (vy - ax_a_y) * unit_x)
                if lateral > half_width:
                    half_width = lateral
        # RUNWAY CROWN (user 2026-07-07 / part 30): the designed edge
        # drop for this runway — the crown drop FIELD assigns it to every
        # ring node (uniform per ref) and the solve's writeback applies
        # it; the profile itself stays the centerline (spine) authority.
        from .crown import runway_crown_drop_m
        crown_drop = runway_crown_drop_m(half_width)
        profiles[ref] = {
            'axis_a': (ax_a_x, ax_a_y),
            'axis_d': (ax_dx, ax_dy),
            'axis_len2': ax_len2,
            'half_width_m': half_width,
            'crown_drop_m': crown_drop,
            'fractions': list(fractions),
            'elevs': list(elevs),
            # anchor provenance for the RUNWAY FLEX pass
            # (docs/runway_flex_plan.md): the flex must respect the
            # CERTAIN anchors (thresholds + seams) while treating the
            # rest as negotiable.
            'anchored': list(anchored),
            'seam_t': [t for (t, _e) in seam_samples],
            'blast_a_m': state['blast_a_m'],
            'blast_b_m': state['blast_b_m'],
            # The (possibly escalated) end-zone cap this profile was
            # solved under.  The flex re-solve MUST reuse it — solving
            # a ref under two different end-zone caps would make the
            # flex re-stamp diverge from the redistributed profile.
            'end_zone_cap': end_zone_cap,
            # TIERED end-zone caps (defect G): the strict threshold-band
            # cap (0.8% unless genuinely infeasible) and the band extent
            # (fraction of length before each threshold).  The flex
            # re-solve must gate identically or its re-stamp diverges.
            'threshold_cap': threshold_cap,
            'threshold_strict_fraction': threshold_strict_fraction,
        }

        # Evaluate the new profile at every runway sub-rect's vertex.
        n_touched += _apply_profile_to_shapes(
            shapes, ax_a_x, ax_a_y, ax_dx, ax_dy, ax_len2,
            fractions, elevs)

    return n_touched


def _apply_profile_to_shapes(shapes, ax_a_x, ax_a_y, ax_dx, ax_dy,
                             ax_len2, fractions, elevs) -> int:
    """Evaluate ``(fractions, elevs)`` at every runway sub-rect vertex
    (projected onto the axis) and write the altitudes back — shared by
    the seam redistribute and the RUNWAY FLEX pass.

    NOTE (SPINE CROWN v2, part 30): shapes carry the PROFILE values —
    the crown drop is NOT baked here.  Runway ring nodes join the crown
    drop field (``crown.build_crown_drop_field``, uniform
    ``profiles[ref]['crown_drop_m']`` per ref) and the SOLVE's writeback
    subtracts it, so every in-solve reader (flex, join anchors, crossing
    reconciliation, seam pins) keeps working in one profile space."""
    n_touched = 0
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
            new_alts.append(round(e_new, 2))

        # Detect whether the new altitudes still form a canonical
        # ``[H, L, L, H]`` 4-corner sloped rect.  If so AND the
        # shape was originally that form, preserve it (keeps the
        # ``sloping_rect_canonical_form`` invariants happy and
        # avoids unnecessary node_altitudes conversions when
        # nothing actually moved).
        # RUNWAYS ARE ALWAYS PER-VERTEX from here on (user 2026-07-06,
        # completing the unified representation: the taxi network moved
        # to spine faces + node_altitudes long ago; runways were the
        # holdout).  The canonical [H, L, L, H] form bound ``hi`` to
        # ring corners 0/3 positionally, and every consumer that
        # re-derived corners from [hi, lo, lo, hi] carried a silent
        # orientation assumption — the third such bug (a flex dip
        # inverts a piece's slope; the 'ensure hi is higher' swap then
        # MIRRORED it into an 8 m tear).  node_altitudes carries the
        # orientation explicitly and to_osm already ships it (23 of 56
        # HECA runway pieces emitted per-vertex before this change).
        closed_alts = new_alts + ([new_alts[0]]
                                   if ring_closed else [])
        s.node_altitudes = closed_alts
        s.altitude = None
        s.altitude_high = None
        s.altitude_low = None
        n_touched += 1

    return n_touched


def flex_slack_at(profile: dict, t: float, direction: float) -> float:
    """How far the profile value at fraction ``t`` may move in
    ``direction`` (+1 up / −1 down) before violating the grade-cap
    envelope of a CERTAIN anchor (threshold ends + tile-seam samples —
    docs/runway_flex_plan.md; intermediate anchors are negotiable by
    the user ruling and impose no slack limit here).

    Conservative pairwise-envelope bound: for every certain anchor i,
    the flexed value v must satisfy ``|v − e_i| ≤ cap·|s_t − s_i|``.
    The K-factor is enforced afterwards by ``faa_joint_solve``'s gates
    on the free samples; the longitudinal-grade validator is the
    backstop.

    SPACE INVARIANT (verified 2026-07-16 with the crowned-edge join
    ruling): every value here is CENTERLINE-PROFILE (uncrowned) space —
    the runway-join anchors sample the profile-valued shapes in-solve,
    the flex demands are computed from those anchors, and the crown drop
    (including the join-anchored nodes' edge drop) is applied only at
    the solve's writeback (``crown.build_crown_drop_field``).  Never
    feed an emitted (crowned) value into this clamp."""
    fractions = profile['fractions']
    elevs = profile['elevs']
    anchored = profile.get('anchored') or [False] * len(fractions)
    seam_t = set(profile.get('seam_t') or ())
    axis_len = math.sqrt(profile['axis_len2'])
    current = _interp_profile(fractions, elevs, t)

    # Bound against EVERY anchored sample — not only the certain ones
    # (thresholds + seams).  Intermediate anchors (crossing
    # reconciliations) are negotiable by the ruling, but until Stage C
    # solves them jointly they stay ANCHORED through the flex re-solve,
    # and a target that contradicts one bakes a step INSIDE the runway
    # (HECA B2 first cut: 75 runway-internal pairs, 2.8 m over 17 m,
    # where flexed targets sat beside frozen crossing anchors).
    del seam_t  # certain/intermediate distinction returns in Stage C
    bounding: List[int] = [k for k, a in enumerate(anchored) if a]

    # TIERED THRESHOLD BAND (user 2026-07-16, KBNA 13/31 defect G): the flex
    # drags the runway toward a taxiway contact, but within the last
    # ``threshold_strict_fraction`` before a pinned CIFP threshold the ramp
    # must stay gentle (≤0.8%) — the threshold is standing law, so a contact
    # in that band cannot pull the profile down at the 1.5% main cap.  Bound
    # a contact against the NEAR threshold anchor at ``RUNWAY_END_GRADE``
    # instead of ``MAX_RUNWAY_GRADE``; the deficit stays a small residual at
    # the taxi join (the taxi yields to the threshold), not a steep runway
    # end.  ``threshold_strict_fraction`` == 0 (untiered) keeps the old bound.
    tsf = float(profile.get('threshold_strict_fraction') or 0.0)
    thr_first = bounding[0] if bounding else None
    thr_last = bounding[-1] if bounding else None

    slack = float("inf")
    for k in set(bounding):
        cap = MAX_RUNWAY_GRADE
        if (tsf > 0.0 and k in (thr_first, thr_last)
                and abs(t - fractions[k]) < tsf):
            cap = RUNWAY_END_GRADE
        distance = abs(t - fractions[k]) * axis_len
        budget = cap * distance
        current_diff = (current - elevs[k]) * direction
        slack = min(slack, budget - current_diff)
    return max(0.0, slack if slack != float("inf") else 0.0)


def apply_runway_flex(layout, demands: Dict[str, list]) -> Dict[str, list]:
    """RUNWAY FLEX Stage B (docs/runway_flex_plan.md): move each runway's
    profile at the given contact positions by the requested amounts,
    re-run the FAA gates, and write the flexed profile back to the
    runway shapes + the persisted profile registry.

    ``demands``: ``{ref: [(t_contact, flexed_value), …]}`` — the caller
    (the solve's flex hook) has already clamped each value into the
    certain-anchor envelope via :func:`flex_slack_at`.  The contact
    samples are inserted ANCHORED so the surrounding free samples
    re-smooth around them under the same grade/K gates the seam
    redistribute uses.  Returns ``{ref: [(t, achieved_value), …]}``.
    """
    from .layout import ROLE_RUNWAY
    profiles = getattr(layout, "_runway_redistributed_profiles", None)
    if not profiles:
        return {}
    shapes_by_ref: Dict[str, list] = defaultdict(list)
    for s in layout.shapes:
        if (s.role == ROLE_RUNWAY and s.polygon is not None
                and not s.polygon.is_empty and s.ref):
            shapes_by_ref[s.ref].append(s)

    achieved: Dict[str, list] = {}
    for ref, contact_list in demands.items():
        profile = profiles.get(ref)
        shapes = shapes_by_ref.get(ref)
        if profile is None or not shapes or not contact_list:
            continue
        original_fractions = list(profile['fractions'])
        original_elevs = list(profile['elevs'])
        original_anchored = list(profile.get('anchored')
                                 or [False] * len(original_fractions))
        axis_len = math.sqrt(profile['axis_len2'])
        ax_a_x, ax_a_y = profile['axis_a']
        ax_dx, ax_dy = profile['axis_d']
        # CROSSING-RECONCILED verts live only in the SHAPES (the
        # reconciliation pass runs after the profile persists): any ring
        # vertex whose value deviates from the profile is such an
        # intermediate anchor.  Fold each into the arrays as ANCHORED
        # (dedup by t) so the flex respects it and the shape re-eval
        # can't stomp it — the 05C×crossing tear (HECA B2: profile
        # evaluation wrote 99.03 over the reconciled 106.49 while the
        # partner kept it → an 8 m tear).  Stage C makes these solvable.
        for s in shapes:
            ring = list(s.polygon.exterior.coords)
            ring_open = (ring[:-1] if ring and ring[0] == ring[-1]
                         else ring)
            alts = s.node_altitudes or []
            if not alts or len(alts) < len(ring_open):
                continue
            for k, (x, y) in enumerate(ring_open):
                t = ((x - ax_a_x) * ax_dx + (y - ax_a_y) * ax_dy) \
                    / profile['axis_len2']
                if not (0.0 < t < 1.0):
                    continue
                value = float(alts[k])
                if abs(value - _interp_profile(original_fractions,
                                               original_elevs, t)) <= 0.05:
                    continue
                matched = False
                for j, frac in enumerate(original_fractions):
                    if abs(frac - t) < 1e-3:
                        original_elevs[j] = value
                        original_anchored[j] = True
                        matched = True
                        break
                if not matched:
                    insert_at = next(
                        (j for j, frac in enumerate(original_fractions)
                         if frac > t), len(original_fractions))
                    original_fractions.insert(insert_at, t)
                    original_elevs.insert(insert_at, value)
                    original_anchored.insert(insert_at, True)
        pending = sorted((t, v) for (t, v) in contact_list
                         if 0.0 < t < 1.0)

        def _solve_with(target_list):
            fractions = list(original_fractions)
            elevs = list(original_elevs)
            anchored = list(original_anchored)
            for t, v in target_list:
                placed = False
                for k, frac in enumerate(fractions):
                    if abs(frac - t) < 1e-3:
                        elevs[k] = v
                        anchored[k] = True
                        placed = True
                        break
                if not placed:
                    insert_at = next(
                        (k for k, frac in enumerate(fractions)
                         if frac > t), len(fractions))
                    fractions.insert(insert_at, t)
                    elevs.insert(insert_at, v)
                    anchored.insert(insert_at, True)
            # Same end-zone cap the redistribute solve used for this
            # ref (possibly escalated above 0.8% — see
            # ``solve_profile_with_minimal_end_zone_cap``): the flexed
            # profile must be gated identically or the flex re-stamp
            # diverges from the redistributed profile.
            faa_joint_solve(
                fractions, elevs, anchored, axis_len,
                blast_a=float(profile.get('blast_a_m') or 0.0),
                blast_b=float(profile.get('blast_b_m') or 0.0),
                grade_cap=MAX_RUNWAY_GRADE,
                end_grade_cap=float(profile.get('end_zone_cap')
                                    or RUNWAY_END_GRADE),
                max_dg_per_m=MAX_RUNWAY_GRADE_CHANGE_PER_M,
                threshold_strict_cap=(
                    float(profile['threshold_cap'])
                    if profile.get('threshold_cap') is not None else None),
                threshold_strict_fraction=float(
                    profile.get('threshold_strict_fraction') or 0.0))
            return fractions, elevs, anchored

        def _worst_over_cap(fractions, elevs):
            return _worst_segment_over_main_cap(fractions, elevs, axis_len)

        # VERIFY-AND-RELAX (2026-07-06): a jointly-infeasible target set
        # leaves faa_hard_cap_pass midpointing squeezed free samples —
        # over-cap segments INSIDE the runway (HECA B2: 05L +7.5 %/2.5 m,
        # 05C +2.0 %/538 m).  The flex must never trade taxi feasibility
        # for runway law: while the re-solved profile has any over-cap
        # segment, drop the target nearest the worst violation and
        # re-solve from the ORIGINAL arrays.  Worst case = no flex.
        while True:
            fractions, elevs, anchored = _solve_with(pending)
            worst = _worst_over_cap(fractions, elevs)
            if worst is None or not pending:
                break
            _excess, midpoint = worst
            drop_index = min(range(len(pending)),
                             key=lambda k: abs(pending[k][0] - midpoint))
            pending.pop(drop_index)
        if _worst_over_cap(fractions, elevs) is not None:
            continue        # even target-free re-solve over cap: keep as-was
        profile['fractions'] = list(fractions)
        profile['elevs'] = list(elevs)
        profile['anchored'] = list(anchored)
        ax_a_x, ax_a_y = profile['axis_a']
        ax_dx, ax_dy = profile['axis_d']
        _apply_profile_to_shapes(
            shapes, ax_a_x, ax_a_y, ax_dx, ax_dy,
            profile['axis_len2'], fractions, elevs)
        achieved[ref] = [(t, _interp_profile(fractions, elevs, t))
                         for (t, _v) in contact_list]
    return achieved

