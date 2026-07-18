"""Whole-airport flat fast path (spec §3.3, Tier 2).

When an airport is provably flat, the route-profile solve's expensive
terrain-dependent stages — the reach bands (:func:`anchors.node_bands` and
the per-building band evaluation in ``building_feasibility``), the spine
profile, the body fill and the feasibility iteration — do no useful work:
every soft node is already feasible and already in grade at its DEM seed.
:func:`certify_flat_airport` proves this with a conservative certificate; when
it holds, :func:`apply_flat_airport_fast_path` seeds every soft node at its DEM
value, runs write-back and emission unchanged, and lets the scoped final grade
projection defer every certified shape.

A :class:`FlatAirportCertificate` HOLDS only when ALL of:

* every soft pavement shape's within-shape grade edges are satisfied at the
  DEM seed — reused from the Tier-0/1 flatness certificates
  (``lazy_certified`` markers on the constraint entries) for the certified
  shapes, and verified directly against the seed for any shape that was not
  certified (runway-adjacent junctions, service junctions);
* every airside-served (and every detached) building footprint's DEM relief
  fits the seat flatness tolerance — buildings are FLAT (owner ruling), so a
  flat footprint seats at its DEM mean by inspection (write-back averages the
  ring), and a non-flat footprint refuses the whole airport;
* every runway's along-axis DEM relief fits the runway profile budgets
  (``RUNWAY_END_GRADE`` in the end zones, ``RUNWAY_MAX_GRADE`` elsewhere) at
  ``FLATNESS_CERTIFICATE_RATE_FACTOR`` margin, sampled through the exact
  ``elevation._sample_dem`` discipline the node seeds use;
* no bridge / tunnel / crossing-terrain / object-pad / portal-crown subsystem
  claimed any geometry at this airport.

Any doubt refuses (fail toward correctness): a refused certificate simply
sends the airport down the normal solve.  Every refusal records a reason
string (``layout._flat_airport_fast_path_reason``) for the per-airport
``[flat-certificate]`` summary line.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import Optional


# Slack the seed-edge verification allows on top of a within-shape budget: the
# validator's emit-rounding noise, so a pair sitting bit-exactly at its budget
# after DEM sampling is not spuriously refused.  Well below any real grade
# feature — fail toward correctness.
_FAST_PATH_EDGE_TOLERANCE_M = 0.03

# Along-axis DEM sampling pitch for the runway relief check (m).  The
# production DEM is airport-smoothed before patch generation, so a step this
# fine cannot straddle an un-spread terrain feature.
_RUNWAY_SAMPLE_PITCH_M = 15.0


@dataclass
class FlatAirportCertificate:
    """A held whole-airport flat certificate (spec §3.3).

    ``certified_counts`` — per soft-shape class ``{class: certified_count}``
    reused from the Tier-0/1 tally, plus ``building`` (flat seats) and
    ``runway`` (runways whose relief fits budget).
    ``runway_relief`` — ``{runway_ref: worst_relief_rate_ratio}`` (diagnostic:
    the along-axis DEM relief as a fraction of the applicable budget · margin).
    ``seed_elevation`` — the full per-node seed the fast path writes back
    (hard/runway/seam at profile values, every soft node at its DEM seed).
    ``join_indices`` — ``{node_index: runway_anchor_value}`` for the taxi↔runway
    join nodes the seed pins hard at the local runway elevation.
    ``refusal_reason`` — always ``None`` for a held certificate (a refusal is
    signalled by :func:`certify_flat_airport` returning ``None`` and stashing
    the reason on the layout)."""

    certified_counts: dict = field(default_factory=dict)
    runway_relief: dict = field(default_factory=dict)
    seed_elevation: list = field(default_factory=list)
    join_indices: dict = field(default_factory=dict)
    refusal_reason: Optional[str] = None


def _refuse(layout, reason: str) -> None:
    """Record ``reason`` on the layout for the counter line and return None."""
    try:
        layout._flat_airport_fast_path_reason = reason
    except (AttributeError, TypeError):                    # pragma: no cover
        pass
    return None


def _subsystem_refusal_reason(layout) -> Optional[str]:
    """Return a refusal reason if any terrain-overriding subsystem claimed
    geometry at this airport, else ``None`` (spec §3.3(c)).

    Bridges, tunnels, crossing-terrain zones, object-derived bridge pads and
    portal crowns all override terrain by design and are excluded from every
    flat certificate.  When in doubt (an attribute exists but its truthiness is
    ambiguous) the caller refuses upstream — this only reports the definite
    presences.
    """
    from auto_patch.layout import (
        ROLE_BRIDGE_CAUSEWAY, ROLE_BRIDGE_TRENCH, ROLE_TUNNEL_RAMP)
    from auto_patch.crossing_terrain import crossing_influence_zone_union

    if crossing_influence_zone_union(layout) is not None:
        return "crossing-terrain zone present"

    bridge_roles = {ROLE_BRIDGE_TRENCH, ROLE_BRIDGE_CAUSEWAY, ROLE_TUNNEL_RAMP}
    for s in layout.shapes:
        if s.role in bridge_roles:
            return "bridge/tunnel plate present"
        if getattr(s, "is_bridge", False):
            return "taxi-bridge span present"

    if getattr(layout, "_object_bridge_pin_values", None):
        return "object-bridge pad present"

    # Per-airport terrain-absorption stores (gap-fill spines, adjacent-ground
    # bands): these are envelope-interval variables whose value + special
    # write-back the fast path cannot soundly reproduce.  The stores are truthy
    # ONLY when the airport actually has that geometry (an empty store is
    # falsy) — so the presence of the ADMISSION gates alone never refuses; only
    # real geometry does.
    if getattr(layout, "gap_fill_presolve", None):
        return "gap-fill spine present"
    if getattr(layout, "adjacent_ground_presolve", None):
        return "adjacent-ground band present"
    return None


def _runway_axes(layout):
    """Yield ``(runway_ref, (ax, ay), (bx, by), length_m)`` per runway — the
    farthest-apart ring-vertex pair of every ``ROLE_RUNWAY`` group (grouped by
    ``ref``) approximates the runway centerline for the along-axis relief
    sweep.  Runways with no measurable extent are skipped."""
    from auto_patch.layout import ROLE_RUNWAY
    from auto_patch.elevation_per_surface.solver_primitives import _open_ring

    groups: dict = {}
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY or s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = _open_ring(list(s.polygon.exterior.coords))
        except (ValueError, TypeError):
            continue
        groups.setdefault(s.ref or "", []).extend(
            (float(x), float(y)) for (x, y) in coords)

    for ref in sorted(groups):
        points = groups[ref]
        best = -1.0
        endpoints = None
        for p in range(len(points)):
            xa, ya = points[p]
            for q in range(p + 1, len(points)):
                xb, yb = points[q]
                dist = math.hypot(xa - xb, ya - yb)
                if dist > best:
                    best = dist
                    endpoints = ((xa, ya), (xb, yb))
        if endpoints is None or best < 1.0:
            continue
        yield ref, endpoints[0], endpoints[1], best


def _runway_relief_ratio(layout, dem, tile_lat, tile_lon, rate_factor):
    """Along-axis DEM relief check for every runway (spec §3.3, second clause).

    Returns ``(relief_by_ref, refusal_reason)``.  For each runway the DEM is
    sampled through ``elevation._sample_dem`` at ``_RUNWAY_SAMPLE_PITCH_M`` along
    the centerline; each consecutive segment's relief must be within
    ``rate_factor · budget · segment_length`` where the budget is
    ``RUNWAY_END_GRADE`` in the first/last ``RUNWAY_END_FRACTION`` of the length
    and ``RUNWAY_MAX_GRADE`` in between.  Any sampling gap refuses (returns a
    reason).  ``relief_by_ref`` maps each runway to its worst relief/budget
    ratio (diagnostic)."""
    from auto_patch.elevation import _sample_dem
    from auto_patch.config import (
        RUNWAY_END_FRACTION, RUNWAY_END_GRADE, RUNWAY_MAX_GRADE)

    relief_by_ref: dict = {}
    saw_runway = False
    for ref, (ax, ay), (bx, by), length in _runway_axes(layout):
        saw_runway = True
        steps = max(1, int(math.ceil(length / _RUNWAY_SAMPLE_PITCH_M)))
        samples = []
        for k in range(steps + 1):
            frac = k / steps
            x = ax + (bx - ax) * frac
            y = ay + (by - ay) * frac
            try:
                lat, lon = layout.m_to_ll(x, y)
                value = _sample_dem(dem, tile_lat, tile_lon, lat, lon)
            except (ValueError, TypeError):
                return relief_by_ref, f"runway {ref} DEM sampling gap"
            if value is None or value != value:
                return relief_by_ref, f"runway {ref} DEM sampling gap"
            samples.append(float(value))
        segment_length = length / steps
        worst_ratio = 0.0
        for k in range(steps):
            mid_frac = (k + 0.5) / steps
            in_end_zone = (mid_frac < RUNWAY_END_FRACTION
                           or mid_frac > 1.0 - RUNWAY_END_FRACTION)
            budget_rate = RUNWAY_END_GRADE if in_end_zone else RUNWAY_MAX_GRADE
            allowed = rate_factor * budget_rate * segment_length
            relief = abs(samples[k + 1] - samples[k])
            if allowed <= 0.0:
                if relief > _FAST_PATH_EDGE_TOLERANCE_M:
                    return (relief_by_ref,
                            f"runway {ref} along-axis relief over budget")
                continue
            ratio = relief / allowed
            if ratio > worst_ratio:
                worst_ratio = ratio
            if relief > allowed + _FAST_PATH_EDGE_TOLERANCE_M:
                return (relief_by_ref,
                        f"runway {ref} along-axis relief over budget")
        relief_by_ref[ref] = worst_ratio
    if not saw_runway:
        return relief_by_ref, "no runway shapes"
    return relief_by_ref, None


def _building_seat_refusal(layout, dem, tile_lat, tile_lon):
    """Return a refusal reason if any building footprint is not flat within the
    seat tolerance, else ``None`` (spec §3.3, buildings are FLAT).

    On a certified-flat airport the reach band is wide by construction (the DEM
    itself is a feasible in-band field everywhere — every soft shape and every
    runway certified flat), so the WP1 band-margin guard on the seat is
    automatically satisfied and is dropped here; the flatness test alone is the
    binding condition.  A building whose footprint DEM relief exceeds the
    tolerance means the airport is not uniformly flat — refuse."""
    from auto_patch.layout import ROLE_BUILDING
    from auto_patch.config import BUILDING_SEAT_FLATNESS_TOLERANCE_M
    from auto_patch.elevation import _sample_dem
    from auto_patch.elevation_per_surface.building_feasibility import (
        _footprint_dem_relief)

    def _sampler(x, y):
        try:
            lat, lon = layout.m_to_ll(x, y)
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except (ValueError, TypeError):
            return None

    count = 0
    for s in layout.shapes:
        if (s.role != ROLE_BUILDING or s.polygon is None
                or s.polygon.is_empty
                or s.polygon.geom_type != "Polygon"):
            continue
        relief = _footprint_dem_relief(s.polygon, _sampler)
        if relief is None:
            return "building footprint DEM sampling gap", count
        if relief[1] > BUILDING_SEAT_FLATNESS_TOLERANCE_M:
            return "building footprint over seat tolerance", count
        count += 1
    return None, count


def _solve_inputs(layout, dem, tile_lat, tile_lon):
    """Build the solve artifacts the certificate reuses when called standalone
    (tests / probes): ``(nodes, bucket_to_idx, elev, base_hard, dem_elev,
    runway_nodes, shape_constraints, unified_graph)``.  Mirrors the setup
    ``solve_route_profile`` performs before it would call the certificate;
    ``None`` if the layout has no solver nodes."""
    from auto_patch import grade_graph as GG
    from auto_patch.elevation_per_surface.solver_primitives import (
        _build_node_list, _build_shape_constraints, _runway_node_set,
        _sample_node_dem, _seed_elevations)

    nodes, bucket_to_idx = _build_node_list(layout)
    if not nodes:
        return None
    elev, base_hard, _have = _seed_elevations(
        layout, nodes, bucket_to_idx, dem=dem,
        tile_lat=tile_lat, tile_lon=tile_lon)
    dem_elev = _sample_node_dem(layout, nodes, dem, tile_lat, tile_lon)
    runway_nodes = _runway_node_set(layout, bucket_to_idx)
    ctx = GG.build_context(layout, bucket_to_idx)
    hard_for_certificate = ({i for i in range(len(elev)) if base_hard[i]}
                            | {i for i in runway_nodes if i < len(elev)})
    shape_constraints = _build_shape_constraints(
        layout, bucket_to_idx, ctx=ctx, dem=dem,
        tile_lat=tile_lat, tile_lon=tile_lon,
        hard_nodes=hard_for_certificate)
    unified_graph = GG.build_unified_graph(layout, bucket_to_idx, ctx=ctx)
    return (nodes, bucket_to_idx, elev, base_hard, dem_elev,
            runway_nodes, shape_constraints, unified_graph)


def _build_seed(elev, base_hard, dem_elev, unified_graph, flexed_idx):
    """Return ``(seed, join_indices)`` — the fast-path seed elevation and the
    taxi↔runway join pins (spec §3.3, "every soft node takes its DEM seed
    value").

    A hard node (runway / seam / CIFP) keeps its profile value; a soft node
    takes its DEM seed.  Then the unified graph's runway-join anchors pin every
    taxi-spine runway-contact node hard at the LOCAL runway elevation (the same
    stamp ``solve_route_profile`` applies), except a FLEXED runway node, which
    keeps its flexed profile value (``flexed_idx`` = ``layout.
    _flexed_runway_node_idx``, possibly empty).  Returns ``(None, {})`` if any
    soft node has no DEM value (sampling gap → the caller refuses)."""
    n = len(elev)
    seed = [0.0] * n
    for i in range(n):
        if base_hard[i]:
            seed[i] = float(elev[i])
        else:
            value = dem_elev[i] if i < len(dem_elev) else None
            if value is None or value != value:
                return None, {}
            seed[i] = float(value)
    join_indices: dict = {}
    flexed = flexed_idx or ()
    for i, runway_elev in unified_graph.runway_anchor.items():
        if i >= n:
            continue
        if base_hard[i] and i in flexed:
            continue
        seed[i] = float(runway_elev)
        join_indices[i] = float(runway_elev)
    return seed, join_indices


def certify_flat_airport(layout, dem, tile_lat: int = 0, tile_lon: int = 0,
                         *, nodes=None, bucket_to_idx=None, elev=None,
                         base_hard=None, dem_elev=None, runway_nodes=None,
                         shape_constraints=None, unified_graph=None,
                         ) -> Optional[FlatAirportCertificate]:
    """Prove (or refuse) that the whole airport is flat (spec §3.3).

    Returns a held :class:`FlatAirportCertificate` when every condition in the
    module docstring holds, else ``None`` (with the reason stashed on
    ``layout._flat_airport_fast_path_reason``).  ``solve_route_profile`` passes
    its already-built artifacts through the keyword-only parameters (reusing the
    Tier-0/1 certificates, never re-deriving); when they are absent (standalone
    callers / tests) the certificate builds them itself via :func:`_solve_inputs`.
    Fail toward correctness: any sampling gap or ambiguity refuses."""
    from auto_patch.config import (
        FLAT_AIRPORT_FAST_PATH, FLAT_CERTIFICATE_COVERAGE,
        FLATNESS_CERTIFICATE_RATE_FACTOR)

    try:
        layout._flat_airport_fast_path_reason = None
    except (AttributeError, TypeError):                    # pragma: no cover
        pass

    if not FLAT_AIRPORT_FAST_PATH:
        return _refuse(layout, "fast-path gate off")
    if dem is None:
        return _refuse(layout, "no DEM")
    # The certificate reuses the Tier-1 coverage markers; without coverage
    # there is nothing to reuse.
    if not (FLAT_CERTIFICATE_COVERAGE
            and os.environ.get("O4_FLAT_CERTIFICATE_COVERAGE", "1") != "0"):
        return _refuse(layout, "certificate coverage off")

    # (c) terrain-overriding subsystems.
    subsystem_reason = _subsystem_refusal_reason(layout)
    if subsystem_reason is not None:
        return _refuse(layout, subsystem_reason)

    # Build / reuse the solve artifacts.
    if shape_constraints is None or unified_graph is None:
        built = _solve_inputs(layout, dem, tile_lat, tile_lon)
        if built is None:
            return _refuse(layout, "no solver nodes")
        (nodes, bucket_to_idx, elev, base_hard, dem_elev,
         runway_nodes, shape_constraints, unified_graph) = built
    if elev is None or base_hard is None or dem_elev is None:
        return _refuse(layout, "missing solve seed")

    rate_factor = FLATNESS_CERTIFICATE_RATE_FACTOR

    # (b) runway along-axis DEM relief.
    relief_by_ref, runway_reason = _runway_relief_ratio(
        layout, dem, tile_lat, tile_lon, rate_factor)
    if runway_reason is not None:
        return _refuse(layout, runway_reason)

    # buildings are FLAT (seat flatness).
    building_reason, building_count = _building_seat_refusal(
        layout, dem, tile_lat, tile_lon)
    if building_reason is not None:
        return _refuse(layout, building_reason)

    # The fast-path seed (soft = DEM, hard/runway/seam = profile, joins pinned).
    seed, join_indices = _build_seed(
        elev, base_hard, dem_elev, unified_graph,
        getattr(layout, "_flexed_runway_node_idx", None))
    if seed is None:
        return _refuse(layout, "soft node DEM sampling gap")

    # (a) every soft shape's within-shape grade is satisfied at the seed.
    # Certified shapes (``lazy_certified``) are proven flat by their Tier-0/1
    # certificate — reused, not re-derived.  Every other soft-shape entry has
    # its full eager edge set; verify each grade edge (budget > 0; the budget-0
    # flat-cross rect pairs are enforced by write-back's planar canonicalise,
    # not by grade) holds at the seed.  Any violation refuses.
    certified_shapes = 0
    for entry in shape_constraints:
        if entry.get("lazy_certified"):
            certified_shapes += 1
            continue
        for edge in entry.get("edges", ()):
            if len(edge) < 3:
                continue
            if len(edge) >= 4:
                # Envelope INTERVAL edge (Stage B0 terrain absorption) — the
                # DEM seed cannot certify it here; refuse (belt-and-braces with
                # the presolve-store checks above).
                return _refuse(layout, "terrain interval constraint present")
            i, j, budget = edge[0], edge[1], edge[2]
            if budget is None or budget <= 1e-9:
                continue                      # flat-cross pair (write-back)
            if i >= len(seed) or j >= len(seed):
                continue
            if abs(seed[i] - seed[j]) > budget + _FAST_PATH_EDGE_TOLERANCE_M:
                return _refuse(layout,
                               "uncertified shape edge over budget")

    counts = getattr(layout, "_flat_certificate_counts", None) or {}
    certified_counts = {
        cls: counts.get(cls, {}).get("certified", 0)
        for cls in ("rect", "apron", "junction")}
    certified_counts["building"] = building_count
    certified_counts["runway"] = len(relief_by_ref)
    certified_counts["shape_entries"] = certified_shapes

    return FlatAirportCertificate(
        certified_counts=certified_counts,
        runway_relief=relief_by_ref,
        seed_elevation=seed,
        join_indices=join_indices,
        refusal_reason=None)


def apply_flat_airport_fast_path(layout, icao, nodes, bucket_to_idx, elev,
                                 base_hard, certificate, t0):
    """Collapse the solve for a certified-flat airport (spec §3.3).

    Writes the certificate's seed elevation onto ``elev`` (every soft node at
    its DEM seed; runway joins pinned hard), runs write-back unchanged, captures
    the scoped-projection snapshot so the pipeline's ``final_grade_projection``
    defers every certified shape, and reports the summary line with
    ``fast-path=TAKEN``.  Mutates ``layout`` (via write-back) and ``elev`` /
    ``base_hard`` in place.  The caller returns from ``solve_route_profile``
    immediately after."""
    from auto_patch.config import CURVE_NATIVE_SPINE, ROUTE_ARC_SPINE
    from auto_patch.elevation_per_surface.solver_primitives import (
        _report, _writeback)

    n = len(elev)
    seed = certificate.seed_elevation
    for i in range(min(n, len(seed))):
        elev[i] = float(seed[i])
    for i in certificate.join_indices:
        if i < n:
            base_hard[i] = True

    n_terms, n_rects, n_juncs = _writeback(layout, elev, bucket_to_idx)

    # Scoped final projection: capture the post-write-back state so the
    # pipeline's ``final_grade_projection`` proves every unchanged apron/junction
    # shape deferrable (Tier-0 machinery) and visits approximately nothing.  The
    # snapshot is only consulted under the global-slice spine + scoped gate — the
    # same guard the projection itself applies.
    if ((CURVE_NATIVE_SPINE or ROUTE_ARC_SPINE)
            and os.environ.get("O4_SCOPED_FINAL_PROJECTION", "1") == "1"):
        from .solve import _capture_projection_snapshot
        try:
            _capture_projection_snapshot(layout)
        except Exception:                                  # pragma: no cover
            pass

    report_flat_certificate_fast_path(layout, icao, "TAKEN", own_tally=True)
    _report(icao, 0, 0, time.time() - t0, n_terms, n_rects, n_juncs)


def report_flat_certificate_fast_path(layout, icao, outcome, *,
                                      own_tally=False):
    """Print the per-airport ``[flat-certificate]`` summary line extended with
    ``fast-path=<outcome>`` (spec §3.3 counter, §2.7 no-silent-caps).

    ``outcome`` is ``"TAKEN"`` (certificate held, fast path ran) or
    ``"refused(<reason>)"``.  ``own_tally`` — on the TAKEN path the fast path
    owns the whole line, so it emits the Tier-0/1 certificate tally here and
    marks ``layout._flat_certificate_reported`` so nothing double-prints; on a
    refusal the normal solve continues to ``building_feasibility``, which prints
    the full tally itself, so only the fast-path token is emitted here."""
    if own_tally:
        from auto_patch.elevation_per_surface.solver_primitives import (
            _report_flat_certificate_counts)
        try:
            _report_flat_certificate_counts(layout, icao)
        except Exception:                                  # pragma: no cover
            pass
    prefix = f"  [flat-certificate] {icao}: " if icao else "  [flat-certificate] "
    print(f"{prefix}fast-path={outcome}")
    if own_tally:
        try:
            layout._flat_certificate_reported = True
        except (AttributeError, TypeError):                # pragma: no cover
            pass
