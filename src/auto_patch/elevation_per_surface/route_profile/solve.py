"""Top-level orchestration for the one-profile elevation solve.

``solve_route_profile`` (docs/one_profile_solve.md) is the ONLY pass that sets
airside elevations.  It reuses the proven, elevation-neutral primitives from
``solver_primitives`` (node list, seed, DEM sample, shape grade graph, level
coupling, writeback) and the route-feasibility building levels from
``building_feasibility`` — then runs the single :func:`one_profile_solve`
(one graph: the taxi-route reach band) over them.

Wiring (``solver.solve`` dispatches here unconditionally):

    nodes/seed/dem  →  reach band + building seats  →  one solve  →  writeback
"""
from __future__ import annotations

import math as _math
import os as _os
import time as _time

from .anchors import (
    apron_body_nodes, build_building_seats, build_nobuilding_apron_seats,
    build_apron_contact_floors, building_spine_floor, node_bands, reach_band_for)
from .one_solve import one_profile_solve


def _apply_runway_flex_hook(layout, icao, nodes, bucket_to_idx, elev,
                            base_hard, shape_constraints, G) -> int:
    """RUNWAY FLEX Stage B2 (docs/runway_flex_plan.md).  Returns the
    number of envelope demands flexed (0 = nothing to do).

    Per runway: seed a value-propagating envelope from ALL OTHER hard
    anchors (other runways' nodes + tile-seam pins) through the law
    graph at FULL legal budgets — the FLEX-LAST condition (taxiways at
    max cap).  Every own node outside its [floor, ceil] is a DEMAND at
    the interval edge (the minimum move); demands thin to ~80 m axis
    bins, clamp to the profile's certain-anchor slack
    (``flex_slack_at``), stay runway-law-consistent between bins, and
    apply through ``apply_runway_flex`` (FAA gates re-run).  Profiles
    iterate toward mutual feasibility (≤3 rounds); runway node seeds +
    the runway-join anchor map re-derive from the flexed shapes.
    """
    import heapq as _heapq
    from auto_patch import grade_graph as _GGf
    from auto_patch.runway_redistribute import (apply_runway_flex,
                                                flex_slack_at)
    from auto_patch.layout import ROLE_RUNWAY

    profiles = getattr(layout, "_runway_redistributed_profiles", None)
    if not profiles or len(profiles) < 2:
        return 0        # single runway (or no profiles) — nothing flexes

    n = len(elev)
    contacts = {i: float(v) for i, v in G.runway_anchor.items() if i < n}
    if len(contacts) < 2:
        return 0

    # full-budget adjacency (the law graph = taxiways at max cap).
    adjacency: dict = {}

    def _add_edge(i, j, budget):
        if budget is None or budget < 0 or i == j:
            return
        adjacency.setdefault(i, []).append((j, budget))
        adjacency.setdefault(j, []).append((i, budget))

    for _sc in shape_constraints:
        for (i, j, budget) in _sc.get("edges", ()):
            _add_edge(i, j, budget)
    for (a, b, cap, _sp) in G.edges:
        if a in G.pos and b in G.pos:
            d = _GGf._dist(G.pos.get(a), G.pos.get(b))
            _add_edge(a, b, cap.at(d, 0.0))

    # ── STAGE B2: envelope-level demands along the WHOLE profile ─────
    # (contact-pair flex drained the contact deficit but the pockets
    # press against every runway node — 2026-07-06 measurement).
    from auto_patch.runway_redistribute import _interp_profile
    from auto_patch.pavement.runway_segments import (
        MAX_RUNWAY_GRADE as _RWY_CAP)

    cps = layout.canonical_points

    def _runway_nodes_for(ref):
        node_set = set()
        for s in layout.shapes:
            if (s.role != ROLE_RUNWAY or (s.ref or "") != ref
                    or s.polygon is None or s.polygon.is_empty):
                continue
            ring = list(s.polygon.exterior.coords)
            for (x, y) in (ring[:-1] if ring and ring[0] == ring[-1]
                           else ring):
                i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
                if i is not None and i < n:
                    node_set.add(i)
        return node_set

    runway_nodes_by_ref = {ref: _runway_nodes_for(ref)
                           for ref in profiles}

    def _reseed_runway_values(ref):
        for s in layout.shapes:
            if (s.role != ROLE_RUNWAY or (s.ref or "") != ref
                    or s.polygon is None or s.polygon.is_empty):
                continue
            ring = list(s.polygon.exterior.coords)
            ring_open = (ring[:-1] if ring and ring[0] == ring[-1]
                         else ring)
            if s.node_altitudes and len(s.node_altitudes) >= len(ring_open):
                per_vertex = s.node_altitudes
            elif (s.altitude_high is not None
                    and s.altitude_low is not None
                    and len(ring_open) == 4):
                per_vertex = [s.altitude_high, s.altitude_low,
                              s.altitude_low, s.altitude_high]
            elif s.altitude is not None:
                per_vertex = [float(s.altitude)] * len(ring_open)
            else:
                continue
            for (x, y), value in zip(ring_open, per_vertex):
                i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
                if i is not None and i < n and base_hard[i]:
                    elev[i] = float(value)

    # node index → owning runway ref, for envelope-origin attribution
    # (which runway's value is PULLING a demand).
    node_owner_ref = {}
    for _ref, _ns in runway_nodes_by_ref.items():
        for _i in _ns:
            node_owner_ref[_i] = _ref

    def _value_envelope(seeds, sign):
        """ceil (sign=+1): min over seeds of value + path budget;
        floor (sign=−1): max of value − path budget.  Strict pop guard
        (no epsilon) — the lazy-Dijkstra re-expansion lesson.

        Returns ``{node: (value, origin_ref)}`` where ``origin_ref`` is
        the runway owning the BINDING seed (None = a non-runway anchor:
        seam pin / building seat / other immovable).  The origin decides
        whether a demand may be SPLIT with the pulling runway (user
        2026-07-06: the deficit divides across the runways pulling on
        it) or must be absorbed in full."""
        best: dict = {}
        _tie = 0                    # heap tiebreaker: origin is not orderable
        pq = []
        for i, v in seeds.items():
            pq.append(((v if sign > 0 else -v), _tie, i,
                       node_owner_ref.get(i)))
            _tie += 1
        _heapq.heapify(pq)
        while pq:
            key, _t, k, origin = _heapq.heappop(pq)
            if k in best:
                continue
            best[k] = ((key if sign > 0 else -key), origin)
            for (j, budget) in adjacency.get(k, ()):
                if j in best:
                    continue
                nt = best[k][0] + sign * budget
                _tie += 1
                _heapq.heappush(
                    pq, ((nt if sign > 0 else -nt), _tie, j, origin))
        return best

    _BIN_M = 80.0
    _DEMAND_TOL_M = 0.05
    # HARD DISPLACEMENT BUDGET (user 2026-07-06: the flex was moving
    # HECA 05C by 17.8 m — far past the minimum): each profile may move
    # at most this far from its ORIGINAL (pre-flex) elevation, summed
    # over all rounds.  The origin-split below is the real law (the
    # deficit divides across the runways pulling on it); this is the
    # safety net against pathological envelope chains.
    from auto_patch.config import RUNWAY_FLEX_MAX_DISPLACEMENT_M
    # matched (fractions, elevs) SNAPSHOT — apply_runway_flex INSERTS
    # samples into the live profile arrays, so interpolating the old
    # elevs against the new fractions indexes out of range.
    original_profiles = {
        ref: (list(profiles[ref]['fractions']),
              list(profiles[ref]['elevs']))
        for ref in profiles if profiles.get(ref)}
    total_deficit = total_drained = 0.0
    n_demands = 0
    flexed_refs: set = set()
    for _round in range(3):
        # SNAPSHOT-SIMULTANEOUS round (user 2026-07-06): demands for ALL
        # runways are computed against the SAME pre-round state, then
        # applied together.  The previous sequential loop re-seeded after
        # each runway, so the FIRST runway absorbed the entire
        # inter-runway deficit (HECA: one-sided 16-17.8 m drops instead
        # of the two profiles meeting in the middle).
        round_targets: dict = {}
        for ref, own_nodes in runway_nodes_by_ref.items():
            if not own_nodes:
                continue
            profile = profiles.get(ref)
            if profile is None:
                continue
            seeds = {i: elev[i] for i in range(n)
                     if base_hard[i] and i not in own_nodes}
            if not seeds:
                continue
            ceil_env = _value_envelope(seeds, +1)
            floor_env = _value_envelope(seeds, -1)
            ax_x, ax_y = profile['axis_a']
            dx, dy = profile['axis_d']
            axis_len2 = profile['axis_len2']
            axis_len = _math.sqrt(axis_len2)
            # per-bin worst demand: (deficit, t, target_value, origin)
            bins: dict = {}
            for i in own_nodes:
                value = elev[i]
                hi, hi_origin = ceil_env.get(i, (None, None))
                lo, lo_origin = floor_env.get(i, (None, None))
                target = origin = None
                if hi is not None and value > hi + _DEMAND_TOL_M:
                    target, origin = hi, hi_origin
                elif lo is not None and value < lo - _DEMAND_TOL_M:
                    target, origin = lo, lo_origin
                if target is None:
                    continue
                x, y = nodes[i]
                t = ((x - ax_x) * dx + (y - ax_y) * dy) / axis_len2
                if not (0.0 < t < 1.0):
                    continue
                deficit = abs(value - target)
                bin_key = int(t * axis_len / _BIN_M)
                if (bin_key not in bins
                        or deficit > bins[bin_key][0]):
                    bins[bin_key] = (deficit, t, target, origin)
            if not bins:
                continue
            # slack-clamp each target, then make consecutive targets
            # runway-law-consistent (anchoring mutually-infeasible
            # targets would bake an over-cap profile).
            candidates = []
            for (deficit, t, target, origin) in bins.values():
                current = _interp_profile(profile['fractions'],
                                          profile['elevs'], t)
                # ORIGIN SPLIT (user 2026-07-06): when the binding
                # anchor is ANOTHER FLEXIBLE RUNWAY, the deficit is a
                # joint obligation — this profile moves only its share
                # (deficit / number of runways pulling = 2 for a pair)
                # and the other runway's own round moves the rest, so
                # the profiles meet in the middle.  An immovable origin
                # (seam pin, building seat, CIFP-adjacent) cannot yield
                # — full move.
                pull = abs(target - current)
                if (origin is not None and origin != ref
                        and origin in profiles):
                    pull = pull / 2.0
                direction = 1.0 if target > current else -1.0
                slack = flex_slack_at(profile, t, direction)
                # cumulative displacement budget vs the ORIGINAL profile
                orig_fr, orig_el = original_profiles.get(
                    ref, (profile['fractions'], profile['elevs']))
                original = _interp_profile(orig_fr, orig_el, t)
                moved_already = abs(current - original)
                budget_left = max(
                    0.0, RUNWAY_FLEX_MAX_DISPLACEMENT_M - moved_already)
                move = min(pull, slack, budget_left)
                if move <= 0.01:
                    continue
                candidates.append((deficit, t,
                                   current + direction * move, move))
            if not candidates:
                continue
            # Mutual consistency by GREEDY KEEP, largest deficit first:
            # a target inconsistent with an already-kept one (over the
            # runway cap between their axis positions) is DROPPED, not
            # forced — forcing dragged a small flex metres past its own
            # slack (HECA B2 first cut: a 2.8 m step INSIDE 05L/23R).
            # Dropped demands retry next round against moved profiles.
            candidates.sort(reverse=True)
            kept = []
            for (deficit, t, value, move) in candidates:
                consistent = all(
                    abs(value - kept_value)
                    <= _RWY_CAP * abs(t - kept_t) * axis_len + 1e-6
                    for (kept_t, kept_value) in kept)
                if not consistent:
                    continue
                kept.append((t, value))
                total_deficit += deficit
                total_drained += move
            if kept:
                round_targets[ref] = sorted(kept)
        if not round_targets:
            break
        for ref, targets in round_targets.items():
            n_demands += len(targets)
            apply_runway_flex(layout, {ref: targets})
            _reseed_runway_values(ref)
            flexed_refs.add(ref)

    if not flexed_refs:
        return 0

    # SHARED-VERTEX PROPAGATION (user 2026-07-06 root-cause): the flex
    # rewrites RUNWAY shapes only, but junctions/aprons stitched to the
    # runway edge carry pre-flex values at the SHARED vertices (stamped
    # by pre-solve reconciliation).  Left stale, the neighbour's value
    # wins the shared bucket at seeding/writeback and re-imposes the
    # pre-flex elevation INTO the flexed runway ring (HECA 05L: one
    # 58.3 vertex in a 61.2 ring = a 24 % runway-internal step).  The
    # flexed runway is the authority at its own edge: re-stamp every
    # coincident vertex on every other shape, and the solver seed.
    flexed_value_by_key: dict = {}
    for ref in flexed_refs:
        for s in layout.shapes:
            if (s.role != ROLE_RUNWAY or (s.ref or "") != ref
                    or s.polygon is None or s.polygon.is_empty
                    or not s.node_altitudes):
                continue
            ring = list(s.polygon.exterior.coords)
            for k in range(min(len(ring), len(s.node_altitudes))):
                if s.node_altitudes[k] is None:
                    continue
                key = cps.get_or_add(float(ring[k][0]), float(ring[k][1]))
                flexed_value_by_key[key] = float(s.node_altitudes[k])
    # Flexed runway hard nodes: the join-anchor loop after this hook
    # must not stamp a sampled "local runway elevation" over them — the
    # sampler reads PIECE geometry and disagrees with the flexed profile
    # at piece ends (HECA 05L: 58.30 stamped over the flexed 61.21 hard
    # node → a 24 % runway-internal step).  The flexed profile IS the
    # local runway surface.
    layout._flexed_runway_node_idx = {
        i for ref in flexed_refs
        for i in runway_nodes_by_ref.get(ref, ()) if i < n}
    n_propagated = 0
    for s in layout.shapes:
        if (s.role == ROLE_RUNWAY or s.polygon is None
                or s.polygon.is_empty or not s.node_altitudes):
            continue
        ring = list(s.polygon.exterior.coords)
        alts = list(s.node_altitudes)
        changed = False
        for k in range(min(len(ring), len(alts))):
            key = cps.get_or_add(float(ring[k][0]), float(ring[k][1]))
            value = flexed_value_by_key.get(key)
            if (value is not None and alts[k] is not None
                    and abs(float(alts[k]) - value) > 0.02):
                alts[k] = value
                changed = True
                n_propagated += 1
        if changed:
            s.node_altitudes = alts
    for key, value in flexed_value_by_key.items():
        i = bucket_to_idx.get(key)
        if i is not None and i < n:
            elev[i] = value
    if n_propagated:
        try:
            import O4_UI_Utils as _UIp
            _UIp.vprint(1, f"  [pav-builder] {icao}: runway flex — "
                           f"re-stamped {n_propagated} shared vertex(es) "
                           f"on neighbouring shapes to the flexed edge.")
        except Exception:
            pass

    G.runway_anchor.clear()
    _GGf._runway_anchors(layout, G, bucket_to_idx)

    try:
        import O4_UI_Utils as _UIf
        _UIf.vprint(1, f"  [pav-builder] {icao}: runway flex (B2) — "
                       f"{n_demands} envelope demand(s), "
                       f"{total_drained:.2f} of {total_deficit:.2f} m "
                       f"drained on {', '.join(sorted(flexed_refs))}.")
    except Exception:
        pass
    return n_demands


def solve_route_profile(layout, icao: str,
                        dem=None, tile_lat: int = 0, tile_lon: int = 0) -> None:
    """Run the one-profile solve and write elevations back onto ``layout``.

    Mutates ``layout`` in place (rect ``altitude_high``/``altitude_low``,
    junction/apron ``node_altitudes``, terminal ``altitude``).  Runway segments
    (HARD anchors) are left untouched.
    """
    from auto_patch.elevation_per_surface.solver_primitives import (
        _build_node_list, _build_shape_constraints, _build_level_coupling,
        _runway_node_set, _sample_node_dem, _seed_elevations, _writeback,
        _report,
    )

    t0 = _time.time()
    nodes, bucket_to_idx = _build_node_list(layout)
    if not nodes:
        return

    elev, base_hard, _have_initial = _seed_elevations(
        layout, nodes, bucket_to_idx, dem=dem,
        tile_lat=tile_lat, tile_lon=tile_lon)
    if not any(base_hard):
        return

    dem_elev = _sample_node_dem(layout, nodes, dem, tile_lat, tile_lon)
    runway_nodes = _runway_node_set(layout, bucket_to_idx)

    # The within-shape grade graph (per-edge cap budgets + rect flat-end pairs)
    # and the rigid flat-across-width coupling — both elevation-neutral.
    from auto_patch.progress import substep as _psub
    _psub(0.20, "Solving elevations — grade graph built")
    # ONE grade context for the whole solve: _build_shape_constraints and
    # build_unified_graph construct identical per-shape GradeShapes, so with a
    # shared ctx the law's pair generation memoises across the two consumers
    # (grade_graph.shape_constraints_cached) instead of running twice.
    from auto_patch import grade_graph as _GG
    _gg_ctx = _GG.build_context(layout, bucket_to_idx)
    # FLATNESS-CERTIFIED LAZY TIER (user 2026-07-05): pass the DEM (the
    # certificate source) and the currently-hard nodes (runway/seam seeds +
    # runway nodes — a shape touching one sits at profile values, never the
    # DEM seed, so it is never certified).
    _hard_for_certificate = ({i for i in range(len(elev)) if base_hard[i]}
                             | {i for i in runway_nodes if i < len(elev)})
    shape_constraints = _build_shape_constraints(
        layout, bucket_to_idx, ctx=_gg_ctx, dem=dem,
        tile_lat=tile_lat, tile_lon=tile_lon,
        hard_nodes=_hard_for_certificate)
    coupling = _build_level_coupling(shape_constraints)

    # ── THE ONE GRAPH (user 2026-06-27) ──────────────────────────────────────
    # Build the unified grade graph ONCE, FIRST — it is the single graph the reach
    # band, the building seats, the spine solve AND the validator all use.  The
    # reach band is now computed ON it (``reach_band_unified``): reachability is a
    # cap-Dijkstra over ``G.spine_adj`` from ``G.runway_anchor``, so the ceiling is
    # the spine's ACHIEVABLE level and cap-consistent by construction — no separate
    # route graph, no ``spine_adjacency`` re-derivation, no ceiling-consistency
    # bridge.  ``G.spine_adj`` already covers every spine node + edge the old
    # ``spine_adjacency`` produced (verified redundant), so the merge is gone.
    G = _GG.build_unified_graph(layout, bucket_to_idx, ctx=_gg_ctx)
    u_spine_adj = G.spine_adj
    # ── RUNWAY FLEX Stage B (user 2026-07-06, docs/runway_flex_plan.md) ──
    # FLEX-LAST: with every route edge at its FULL legal budget (= the
    # taxiways at max cap), find runway-contact pairs whose value gap
    # exceeds the budget-metric shortest path — connections that stay
    # infeasible even with taxiways maxed.  Only CIFP thresholds + tile
    # seams are CERTAIN; the profiles flex toward each other by the
    # MINIMUM (deficit split by available certain-anchor slack), through
    # the same FAA gates the seam redistribute uses.  Runway node seeds
    # and the runway-join anchors are re-derived from the flexed shapes.
    # DEFAULT ON (user 2026-07-06, for in-sim evaluation): Stage B2
    # envelope demands + the inversion fix measured clean at HECA
    # (actionable 11 with ZERO runway pairs; quarantine 11,265 → 2,762)
    # and the flex is a structural no-op on single-runway airports.
    # O4_RUNWAY_FLEX=0 restores frozen profiles.
    if _os.environ.get("O4_RUNWAY_FLEX", "1") == "1" and G.runway_anchor:
        try:
            _n_flexed = _apply_runway_flex_hook(
                layout, icao, nodes, bucket_to_idx, elev, base_hard,
                shape_constraints, G)
        except Exception as _flex_exc:
            import O4_UI_Utils as _UI_flex
            _UI_flex.vprint(1, f"  [pav-builder] WARN: {icao}: runway "
                               f"flex pass failed ({_flex_exc}) — "
                               f"profiles stay frozen.")
    band, dem_fn, runway_pts, _G = reach_band_for(
        layout, elev, bucket_to_idx, dem, tile_lat, tile_lon, unified_graph=G)
    node_band = node_bands(nodes, band)
    _psub(0.55, "Solving elevations — reach bands computed")
    building_seats = build_building_seats(
        layout, bucket_to_idx, band, dem_fn, runway_pts)
    # FEEDER CONVERGENCE (user directive #3): seat each NO-BUILDING apron flat at a
    # single level its feeders can all reach (the ring-band intersection, clamped to
    # DEM), so the feeders converge to it instead of arriving incompatible.  Merged
    # below into ``building_seats`` AFTER ``building_spine_floor`` (which is a
    # building-pad chord model) so apron seats ride the same heaviest-anchor
    # machinery without perturbing the building-frontage spine floor.
    apron_seats = build_nobuilding_apron_seats(layout, bucket_to_idx, band, dem_fn)
    apron_body = apron_body_nodes(layout, bucket_to_idx)

    # NO-BUILDING APRON FILL (user 2026-06-26): a no-building apron has no pad to
    # anchor it, so where the DEM is wrong-low it sags below the level its feeder
    # taxiways can reach.  Seat each such apron FLAT at the closest-DEM level
    # reachable from ALL its routes (filled above the bad DEM) so the taxiways
    # grade smoothly from their runway anchors to it.  Treated like building seats
    # (heaviest anchors); spine nodes that cross the apron keep their taxi grade.
    # NO-BUILDING APRON FILL (user 2026-06-26): the closest-DEM level reachable
    # from all routes, per no-building apron.  Applied below as a per-node FLOOR
    # (raising node_band) so a no-building apron can't sag below its reachable
    # level into a wrong-low DEM pit, while still rising to follow a higher local
    # network (so it does not drag a high-route junction down).

    # ── THE ONE GRAPH (user 2026-06-26, docs/goal_merge_one_graph.md) ─────────
    # Solve the spine DIRECTLY on the geometry nodes the validator checks
    # (``grade_graph.build_unified_graph``) — there is no separate route graph and
    # no read-by-index bridge.  The spine is smoothed (never hard-frozen at an
    # over-cap profile value), runway-adjacent geometry nodes anchor at their OWN
    # LOCAL runway elevation, and a final feasibility-projection drives every
    # grade-graph edge ≤cap (only edges between two hard anchors — runway/building
    # — are left, the genuine steps).  This is what makes the validator's spine
    # zero: build and validate use the exact same nodes.
    if True:
        from .one_solve import feasibility_project
        # G + u_spine_adj already built above (before seating).
        n = len(elev)
        # Runway anchors: every geometry node a taxi spine joins the runway at is
        # HARD at the LOCAL runway elevation (the single hard anchor; the building
        # floor yields).  Never override an existing CIFP/seam hard value (it IS
        # the local runway surface there).
        # Anchor the node the validator's runway-join picks (nearest to each
        # taxi-centerline runway contact) at the LOCAL runway elevation — even if
        # it is already a hard runway node: at a runway INTERSECTION the crossing
        # node sits at a compromise between the two runways (694.8), but the taxi
        # that contacts ONE of them must meet THAT runway's surface (695.3).  ``re``
        # is the runway profile, so this is a no-op for a true runway-end node and
        # only corrects the intersection-compromised crossing node.
        # hard-anchor CATEGORY map (debug: names each hard node's origin in the
        # O4_DUMP_SOLVE_STATE snapshot — the phantom-anchor forensics).
        _hard_cat = {i for i in range(n) if base_hard[i]}
        _hard_cat = {i: "seed_rwy_seam" for i in _hard_cat}
        # FLEXED runway nodes keep the flexed profile value: the join
        # anchor is SAMPLED from piece geometry and disagrees with the
        # flexed profile at piece ends (user 2026-07-06 root-cause —
        # 58.30 stamped over the flexed 61.21 → 24 % inside 05L).
        _flexed_idx = getattr(layout, "_flexed_runway_node_idx", None) or ()
        for i, re in G.runway_anchor.items():
            if i < n:
                if i in _flexed_idx and base_hard[i]:
                    _hard_cat.setdefault(i, "rwy_flexed")
                    continue
                elev[i] = float(re)
                base_hard[i] = True
                _hard_cat.setdefault(i, "rwy_join")
        u_spine_nodes = set(u_spine_adj) | G.spine_nodes()
        # Building-frontage spine floor (the serving arm climbs to its pads),
        # cap-Lipschitz on the unified spine chain.
        u_spine_floor = building_spine_floor(
            layout, nodes, bucket_to_idx, building_seats, node_band, u_spine_adj)
        # APRON-CONTACT FLOOR (user 2026-06-29): a taxiway/junction that meets a
        # BUILDING-anchored apron's edge FAR from the building gets no building floor
        # (>corridor) and no no-building seat (skipped for building aprons), so it
        # solves to its own low DEM and the senior apron cliffs down to it (OEMA TX8
        # #275 → 96 % apron step).  Floor each such feeder contact at the apron's own
        # reachable level so the taxi spine grades UP to the apron — the apron keeps
        # its cap, the taxi yields (the documented apron-owned authority).  Merged as
        # a floor (max), so it composes with the building-frontage floor.
        for _i, _fl in build_apron_contact_floors(
                layout, bucket_to_idx, band, dem_fn, building_seats).items():
            if _fl > u_spine_floor.get(_i, -float("inf")):
                u_spine_floor[_i] = _fl
        # FEEDER CONVERGENCE (tilt model): a no-building apron is ANCHORED like a
        # building so its feeder SPINES grade to meet it — but at the per-feeder
        # feasible level L_i (the apron tilts ≤cap between contacts, see
        # build_nobuilding_apron_seats), NOT one flat level.  Each L_i is in its
        # feeder's reach band, so the spine reaches it without an over-cap step (the
        # earlier FLAT hard seat forced unreachable levels → regressed
        # cyxy_spine_zero + HECA runway; the per-contact tilt level does not).
        building_seats.update(apron_seats)
        # SEAM PINS ARE NEVER SEATS (user 2026-07-04, "treat the seam like
        # a runway edge or building"): a seat level computed at a tile-seam
        # terrain pin overwrites the pin everywhere seats are applied
        # (spine stamp, body fill) and detaches the boundary from the
        # terrain it must meet — SPLP's band-edge corner was seated 66.3
        # over its 63.5 pin.  The pin anchors that node; the apron's other
        # contacts still seat, and the surface grades between them.
        _seam_pin_idx = getattr(layout, "_seam_pin_idx", None) or set()
        for _i in list(building_seats):
            if _i in _seam_pin_idx:
                del building_seats[_i]
        # A building seat that IS a spine node (a pad node on a taxi centerline)
        # is anchored at its ACTUAL seat level DURING the spine solve — so the
        # spine grades its neighbours to within cap of the building (buildings are
        # heaviest).  Otherwise the spine grades to the softer floor (715.35) and
        # PHASE B then slams the seat to its real level (715.63), breaking the cap
        # to the neighbour (the 5.4% junction).  The seat and the spine now agree.
        # (Seam pins were already removed from ``building_seats`` above —
        # the extra guard here is belt-and-braces.)
        for i, lv in building_seats.items():
            if i < n and lv is not None and i in u_spine_adj \
                    and i not in _seam_pin_idx:
                elev[i] = float(lv)
                base_hard[i] = True
                _hard_cat.setdefault(i, "seat_on_spine")

        # SEAM SPINE ANCHORS (user 2026-06-28): where a taxi centerline crosses a
        # tile seam, pin the nearest SPINE node to the SMOOTHED seam DEM as a HARD
        # anchor — so the spine solve below SPREADS the route→seam drop along the
        # centerline (≤cap over its length) instead of leaving the spine at the
        # plateau level and the body cliffing to the seam.  The seam is terrain-
        # pinned for cross-tile stitching; both tiles' route reaches the same seam
        # value → no cross-tile cliff AND no within-apron cliff.  Wires the
        # otherwise-dead ``SEAM_FIELD_ANCHORS`` concept onto the unified graph.
        from auto_patch.config import SEAM_FIELD_ANCHORS
        _cut_lines = getattr(layout, "_seam_cut_lines", None) or []
        if SEAM_FIELD_ANCHORS and dem is not None and _cut_lines:
            _seam_spine_anchors(layout, G, u_spine_adj, elev, base_hard,
                                dem, tile_lat, tile_lon, _cut_lines)

        # TRUTH anchors — everything hard BEFORE the phase-A spine freeze
        # (runway/CIFP + tile-seam DEM pins + runway joins + building spine
        # seats).  The spine-yield projection below may move any node NOT in
        # this set.
        truth_hard = {i for i in range(n) if base_hard[i]}
        for i in truth_hard:
            _hard_cat.setdefault(i, "seam_spine_anchor")
        # PHASE A — dedicated SMOOTH spine solve on the unified graph (geometry
        # nodes), runway/seam HARD at their LOCAL value, building floors honoured.
        # The spine is min-curvature and ≤cap by construction, then FROZEN so the
        # body grades to it (the body twists to meet the spine, never the reverse).
        frozen = _solve_spine_profile(
            elev, base_hard, u_spine_adj, u_spine_floor, node_band,
            nodes_xy=nodes)
        for i in frozen:
            if i < n:
                base_hard[i] = True
        _psub(0.62, "Solving elevations — spine profile solved")

        # Seat every sloping taxi RECT as a flat-ended tilted plane (read from the
        # solved spine), freezing its corners so the body grades to it.  Returns
        # each rect's plane (end node indices) for the final cap re-stamp.
        rect_planes = _flatten_rect_ends(
            layout, bucket_to_idx, elev, base_hard, frozen)

        # PHASE B — body fill (apron/junction interiors + rect bodies + caps) with
        # the spine frozen.  Apron body = 1% VISIBILITY/GEODESIC smoothing within
        # the reach band [floor, ceiling] (apron_smooth=True) — graded ≤1% from its
        # anchored edges/spine, NOT draped on raw DEM bumps (user 2026-06-26).  The
        # band still fills it to the reachable level (west apron → ~693).
        n_free = one_profile_solve(
            elev, shape_constraints, base_hard, nodes, dem_elev,
            runway_nodes, building_seats, apron_body, u_spine_nodes, u_spine_adj,
            node_band, u_spine_floor, coupling, apron_smooth=True)
        _psub(0.78, "Solving elevations — body fill solved")
        # Guarantee compliance: project EVERY grade-graph edge ≤cap with the
        # spine + runway + buildings + seams HARD; only the apron/junction body
        # flexes.  Edges left over cap have both ends hard = genuine steps.
        hard = {i for i in range(n) if base_hard[i]}
        hard |= {i for i in runway_nodes if i < n}
        hard |= {i for i in building_seats if i < n}
        rem, bh = feasibility_project(elev, shape_constraints, hard)
        # Project on the UNIFIED graph's OWN edges too (the EXACT pairs/caps the
        # validator checks — rects/caps all-pair, which shape_constraints only
        # approximates with axial edges), so build and validate cannot leave a
        # residual between them.  The spine stays HARD; only body nodes flex.
        u_edges = [(a, b, cap.at(_GG._dist(G.pos.get(a), G.pos.get(b)), 0.0))
                   for (a, b, cap, _sp) in G.edges
                   if a in G.pos and b in G.pos]
        # NEAR-MISS BUILDING-FRONTAGE LAW EDGES (2026-07-08): pad ↔ apron
        # near-miss edge endpoints, budget = APRON_MAX_GRADE·d — the value-
        # agreement law across a sub-metre unpaved source-offset sliver (SPJC
        # building29).  The phase-A/B floors alone don't survive the
        # projections (min-displacement POCS knows caps, not floors, and
        # projects the lift away); as u_edges members these pairs are
        # enforced by every projection INCLUDING the movable-pad final yield
        # GS, which settles pad level and apron edge JOINTLY (pad stays a
        # rigid flat group).  Gate O4_BUILDING_FRONTAGE_NEAR_MISS=0 → no
        # edges, byte-identical.  See anchors.near_miss_building_frontage_edges.
        from .anchors import near_miss_building_frontage_edges
        u_edges.extend(near_miss_building_frontage_edges(
            layout, bucket_to_idx, building_seats))
        rem, bh = feasibility_project(elev, [{"edges": u_edges}], hard)
        # FINAL re-stamp: continue each end-cap as a planar extension of its
        # parent rect's FINAL plane (rect ends may have flexed in feasibility),
        # skipping any cap corner the spine already owns — done LAST so nothing
        # moves it (the route-graph path's _restamp_caps, on geometry nodes).
        _restamp_caps_unified(layout, bucket_to_idx, elev, rect_planes, frozen)
        # GROUNDSIDE REACH + MOUTH WELD (user 2026-06-27).  Done LAST — after the
        # body solve + feasibility project — so buildings + aprons are anchored:
        #   1. Re-level each groundside piece a service road connects to an apron, to
        #      the elevation the connector can REACH within the service-road grade
        #      cap (apron mouth ± cap·route_len, clamped toward DEM) — so the
        #      connector grades <=cap instead of ramping steeply to the raw DEM.  A
        #      piece with no apron-connected service road stays DEM.
        #   2. Weld each service-road connector mouth to the (re-levelled) groundside
        #      altitude, so connector and groundside emit as ONE node (no cliff).
        # Gate off → elev + groundside untouched → byte-identical.
        _gs_hard = set()
        if _os.environ.get("O4_GROUNDSIDE_MOUTH_ANCHOR", "1") == "1":
            from auto_patch.config import SERVICE_ROAD_MAX_GRADE
            from .anchors import apply_groundside_reach
            _nrl, _gs_hard = apply_groundside_reach(
                layout, bucket_to_idx, elev, SERVICE_ROAD_MAX_GRADE)
            if _gs_hard:
                # The truck route (apron arm + connector + groundside mouth) is now
                # pinned on its rising <=cap profile; re-project so the apron BODY
                # grades into the raised arm and nothing else exceeds its cap.
                _ghard = hard | {i for i in runway_nodes if i < n} | _gs_hard
                feasibility_project(elev, shape_constraints, _ghard)
                feasibility_project(elev, [{"edges": u_edges}], _ghard)
            # Service roads FOLLOW DEM at <=cap (a ground road climbs toward terrain,
            # anchored only at its airside/groundside welds) — SVC4 was held flat in
            # the bowl ~6-11 m below DEM.
            from .anchors import apply_service_road_dem_follow
            _svc_moved = apply_service_road_dem_follow(
                layout, bucket_to_idx, elev, dem_elev, SERVICE_ROAD_MAX_GRADE,
                anchor_extra=_gs_hard)
            if (_nrl or _svc_moved) and _os.environ.get("O4_STEP_DEBUG") == "1":
                print(f"  [groundside-reach] {icao}: re-levelled {_nrl} "
                      f"groundside piece(s); pinned {len(_gs_hard)} route node(s); "
                      f"DEM-followed {len(_svc_moved)} service node(s).")
        _psub(0.88, "Solving elevations — feasibility projection")
        # SPINE-YIELD projection (global-slice spine adaptation, 2026-07-02).
        # Under the global slice most graph nodes ARE spine (every face is
        # born from a centerline cut), so "both ends frozen = genuine step"
        # no longer holds: two route chains solved independently in PHASE A
        # can freeze 2.6 m apart one ring-edge from each other (SPJC measured
        # 1622→3034 frozen-spine/spine residual edges).  Re-project with only
        # the TRUTH anchors hard — runway/CIFP, tile-seam DEM pins, building
        # seats, groundside truck-route pins — so the frozen profiles yield
        # minimally where they disagree.  Runs LAST (after the groundside
        # reach block, whose own re-projections hold the full frozen spine
        # and would otherwise re-wall what an earlier yield fixed), right
        # before writeback.  The phase-A profile is the seed, so smooth
        # spines stay smooth wherever they were already feasible.
        # Rect-model builds keep the legacy behaviour (global-slice only).
        from auto_patch.config import CURVE_NATIVE_SPINE, ROUTE_ARC_SPINE
        if CURVE_NATIVE_SPINE or ROUTE_ARC_SPINE:
            yield_hard = (truth_hard
                          | {i for i in runway_nodes if i < n}
                          | {i for i in building_seats if i < n}
                          | {i for i in _gs_hard if i < n})
            # Fast Jacobi first (bulk of the correction), then the FINAL pass
            # as scalar Gauss-Seidel POCS on the joint edge set — Jacobi has no
            # convergence guarantee and stalls with ~2.5k edges marginally over
            # cap (the audit's POCS on the same polytope reaches ~0 in <100
            # sweeps).  Joint set: projecting the two graphs alternately
            # un-does one with the other.
            rem, bh = feasibility_project(elev, shape_constraints, yield_hard)
            rem, bh = feasibility_project(elev, [{"edges": u_edges}],
                                          yield_hard)
            # MOVABLE FLAT PADS (user 2026-07-03): building pads leave the
            # hard set and become rigid flat GROUPS the projection may move —
            # the audit proves the polytope is feasible ONLY when buildings
            # can move (holding every pre-picked seat hard is infeasible
            # through chained paths: pad↔spine↔pad, even with 0 both-hard
            # edges).  Each pad stays FLAT (the invariant) at a level the
            # projection chooses jointly with the field.
            from auto_patch.layout import ROLE_BUILDING as _RB
            pad_groups = []
            _cps = layout.canonical_points
            if _os.environ.get("O4_YIELD_MOVABLE_PADS", "1") == "1":
                for _s in layout.shapes:
                    if (_s.role != _RB or _s.polygon is None
                            or _s.polygon.is_empty):
                        continue
                    _ring = list(_s.polygon.exterior.coords)
                    _g = {bucket_to_idx.get(_cps.get_or_add(float(x), float(y)))
                          for (x, y) in _ring}
                    # Seam pins never join a movable group (they are
                    # immovable terrain anchors — see yield_hard below).
                    _g = {i for i in _g
                          if i is not None and i < n and i in building_seats
                          and i not in _seam_pin_idx}
                    if len(_g) >= 2:
                        pad_groups.append(_g)
            if pad_groups:
                _pad_nodes = set().union(*pad_groups)
                yield_hard = yield_hard - _pad_nodes
            # NON-PAD SEAT ANCHORS (nobuild-apron tilt seats + contact seats,
            # and seat nodes not on any pad ring) also leave the hard set for
            # the FINAL pass: held hard they oscillate against the runway
            # profile exactly like pads did (measured: worst residual 1.0 m →
            # 0.02, SPJC law-true 406 → ~180).  They still anchored phases
            # A/B, so the surface is already shaped by them; the final GS
            # only relaxes the last-mile conflicts.  Same gate as pads.
            if (pad_groups
                    and _os.environ.get("O4_YIELD_FREE_APRON_SEATS", "1")
                    == "1"):
                yield_hard = yield_hard - (
                    {i for i in building_seats if i < n} - _pad_nodes)
            # SEAM PINS NEVER LEAVE THE HARD SET (user 2026-07-04): the
            # movable-pads / free-apron-seats relaxations above may have
            # freed a node that is ALSO a tile-seam terrain pin — but the
            # seam is a graded-TO anchor exactly like a runway edge; a
            # freed pin lets the final GS park the boundary off the
            # terrain it must meet (SPLP: 0.7 m float at the band edge).
            yield_hard |= {i for i in _seam_pin_idx if i < n}
            joint = list(shape_constraints) + [{"edges": u_edges}]
            # DEBUG snapshot (O4_DUMP_SOLVE_STATE=<path>): pickle the final-
            # projection inputs so projection variants iterate OFFLINE (~1 s)
            # instead of via full rebuilds (~115 s).  Node lat/lon included so
            # an offline scorer can map emitted-patch nids to solver indices.
            _dump = _os.environ.get("O4_DUMP_SOLVE_STATE")
            if _dump:
                import pickle
                _ll = [layout.m_to_ll(x, y) for (x, y) in nodes]
                _cat = dict(_hard_cat)
                for i in runway_nodes:
                    if i < n:
                        _cat.setdefault(i, "runway_node")
                for i in building_seats:
                    if i < n:
                        _cat.setdefault(i, "seat")
                for i in _gs_hard:
                    if i < n:
                        _cat.setdefault(i, "gs_pin")
                with open(_dump, "wb") as _fh:
                    pickle.dump({
                        "elev": list(elev),
                        "joint_edges": [tuple(e) for sc in joint
                                        for e in sc["edges"]],
                        "yield_hard": set(yield_hard),
                        "pad_groups": [set(g) for g in pad_groups],
                        "nodes_m": list(nodes),
                        "nodes_ll": _ll,
                        "dem_elev": list(dem_elev),
                        "node_band": list(node_band),
                        "hard_cat": _cat,
                        # Spine graph (per-edge cap budgets) + runway
                        # anchors: lets an offline probe audit whether a
                        # node's solved level equals its cap-reachable
                        # ceiling (Dijkstra over budgets from anchors).
                        "spine_adj": {int(i): [(int(j), float(b))
                                               for (j, b) in lst]
                                      for i, lst in u_spine_adj.items()},
                        "runway_anchor": {int(i): float(a) for i, a
                                          in G.runway_anchor.items()},
                    }, _fh)
                print(f"    [dump] solve state -> {_dump}")
            # 2400 sweeps: with tightest-budget edge dedup the polytope is
            # consistent and the scalar GS CONVERGES (SPJC: worst residual
            # 0.025 at 800 sweeps → 0.0000 at 1702; the old "oscillation
            # plateau" was the first-edge-wins dedup enforcing conflicting
            # duplicate budgets).  Cap with headroom; the loop exits early
            # at tol.
            _scoped_gate = (_os.environ.get(
                "O4_SCOPED_FINAL_PROJECTION", "1") == "1")
            # Capture the BROKEN quarantine (genuine anchor contradictions,
            # full-graph detection) for the scoped final projection — its
            # sparser graph can miss the same contradictions and grind POCS
            # on the infeasible pockets instead (measured: CYXY 66 k → 11.5 M
            # worklist visits).  Keys, not indices: the projection rebuilds
            # its own node list.
            _solve_broken_idx: set = set()
            rem, bh = feasibility_project(elev, joint, yield_hard,
                                          force_scalar=True, max_iters=2400,
                                          flat_groups=pad_groups or None,
                                          broken_out=(_solve_broken_idx
                                                      if _scoped_gate
                                                      else None))
            # MOUTH VERIFY-AND-RELAX (user 2026-07-06, HECA #541/#546): the
            # groundside mouth welds (``_gs_hard``) were pinned from lot
            # rings computed BEFORE this movable-pad/free-seat yield — a
            # building pad or apron body that settles at a different level
            # leaves a road-ring edge pad↔mouth over-cap with BOTH ends
            # effectively hard (mutually conflicting weld authorities; the
            # DEM-follow break blend cannot fire on a sliver with no
            # interior nodes).  Verify every welded mouth against the joint
            # law edges; where violated, FREE the whole mouth cluster (ONE
            # authority: the joint solve), re-project warm, and have the
            # LOT adopt the projected mouth profile (exact at freed
            # vertices, cap-decay fill, chord-limited) so road and lot
            # still emit as one lawful welded surface.
            if _gs_hard and _os.environ.get(
                    "O4_MOUTH_VERIFY_RELAX", "1") == "1":
                _VIOL_TOL_M = 0.03
                _conflicted: set = set()
                for _sc in joint:
                    for _e in _sc["edges"]:
                        _a, _b, _bud = _e[0], _e[1], _e[2]
                        if (_a >= n or _b >= n
                                or (_a not in _gs_hard
                                    and _b not in _gs_hard)):
                            continue
                        # weld↔weld edges are the LOT↔LOT class — the
                        # reach-time reconciliation owns those (both lots
                        # are final there); freeing both welds here lets
                        # the solve drag them apart (HECA #522: 0.8 m →
                        # 2.1 m after a both-weld free).
                        if _a in _gs_hard and _b in _gs_hard:
                            continue
                        if abs(elev[_a] - elev[_b]) > _bud + _VIOL_TOL_M:
                            if _a in _gs_hard:
                                _conflicted.add(_a)
                            if _b in _gs_hard:
                                _conflicted.add(_b)
                if _conflicted:
                    from .anchors import (adopt_projected_mouths,
                                          expand_mouth_cluster)
                    _freed = expand_mouth_cluster(
                        layout, bucket_to_idx, _conflicted, _gs_hard)
                    yield_hard = yield_hard - _freed
                    rem, bh = feasibility_project(
                        elev, joint, yield_hard, force_scalar=True,
                        max_iters=1200, flat_groups=pad_groups or None)
                    _n_adopted = adopt_projected_mouths(
                        layout, bucket_to_idx, elev, _freed, _gs_hard)
                    # A relaxed mouth is a solver-DECLARED authority-
                    # conflict pocket: export it to the break quarantine
                    # (a fully reconciled mouth has no over-cap pairs, so
                    # the export is inert there; a residual blend — e.g.
                    # the lot ring the adoption re-shaped around the
                    # solved mouth — is quarantined honestly instead of
                    # reading as an actionable solver miss).
                    _solve_broken_idx |= {i for i in _freed if i < n}
                    if _os.environ.get("O4_STEP_DEBUG") == "1":
                        print(f"    [mouth-relax] {len(_conflicted)} "
                              f"conflicted weld(s) → freed cluster "
                              f"{len(_freed)}; {_n_adopted} lot ring(s) "
                              f"adopted the solved profile")
                # WELD↔WELD residuals (HECA #522): two lots' mouth welds on
                # one road ring can still contradict after the reach-time
                # lot↔lot reconciliation (later passes move the field the
                # reconciliation measured against).  Both ends are truth
                # welds — neither may yield — so a still-violated edge is
                # a genuine break pocket: export both mouths.
                _n_weld_pocket = 0
                for _sc in joint:
                    for _e in _sc["edges"]:
                        _a, _b, _bud = _e[0], _e[1], _e[2]
                        if (_a >= n or _b >= n
                                or _a not in _gs_hard
                                or _b not in _gs_hard):
                            continue
                        if abs(elev[_a] - elev[_b]) > _bud + _VIOL_TOL_M:
                            _solve_broken_idx.add(_a)
                            _solve_broken_idx.add(_b)
                            _n_weld_pocket += 1
                if _n_weld_pocket and _os.environ.get(
                        "O4_STEP_DEBUG") == "1":
                    print(f"    [mouth-relax] {_n_weld_pocket} weld↔weld "
                          f"edge(s) still contradictory → break export")
            # EDGE FAIRING (user 2026-07-04, CYXY taxiway E): the spine
            # fairing law covers spine CHAINS only — a corridor's ring
            # EDGE still tracks noise in legal ±cap wiggles (E's edge
            # alternated +2.3 %/+0.8 % every 12 m around a 1.55 % mean).
            # Apply the same second-difference POCS to STRAIGHT boundary
            # runs of airside rings (corners are real grade breaks —
            # skipped by the bend test; anchors never move; band-clamped).
            # SCOPED FINAL PROJECTION (user 2026-07-05): the edge fairing is
            # the ONE pass between the yield projection (which enforced every
            # pair) and the writeback that moves nodes WITHOUT re-enforcing
            # their pairs — record which nodes it moved so the scoped
            # projection treats their shapes as changed (the "unchanged ⇒
            # already enforced" proof does not cover fairing-perturbed nodes).
            _pre_fairing_elev = list(elev) if _scoped_gate else None
            if _os.environ.get("O4_EDGE_FAIRING", "1") == "1":
                from auto_patch.config import TAXIWAY_MAX_GRADE_CHANGE_PER_M
                _n_ekink = _fair_ring_edges(
                    layout, elev, bucket_to_idx, yield_hard, node_band,
                    TAXIWAY_MAX_GRADE_CHANGE_PER_M)
                if _os.environ.get("O4_STEP_DEBUG") == "1":
                    print(f"    [edge-fairing] residual kinks={_n_ekink}")
            _fairing_moved_keys = None
            if _pre_fairing_elev is not None:
                _fairing_moved_keys = {
                    key for key, i in bucket_to_idx.items()
                    if elev[i] != _pre_fairing_elev[i]}
        _psub(0.97, "Solving elevations — writing back")
        # ── SPINE CROWN v2 (user 2026-07-07, part 30) ────────────────────
        # The whole solve above ran in UNCROWNED space z′.  The crown is a
        # designed sub-cap offset field c (crown.build_crown_drop_field):
        # writeback emits z = z′ − c.  Because c is single-valued per
        # canonical node, welds stay consistent; because the law reads the
        # pair offset o_ab = c_b − c_a (grade_law.crown_pair_offset), the
        # emitted surface satisfies |Δz − o| ≤ budget exactly where the
        # solve satisfied |Δz′| ≤ budget — solver and validator share ONE
        # field (exported via the axes sidecar).  Terrain/value contracts
        # (seam pins, building seats, groundside mouth welds, seam spine
        # anchors) are frozen at c = 0.  RUNWAY ring nodes crown through
        # this same transform (uniform per-ref drop) — every in-solve
        # reader (flex, join anchors, crossings, seam pins) sees the one
        # uncrowned profile space, and the emitted edges sit at
        # profile − drop while the spine breakline carries the profile.
        from auto_patch.config import ENABLE_SPINE_CROWN as _CROWN_ON
        from shapely.errors import (GEOSException as _CrGE,
                                    TopologicalError as _CrTE)
        _GEOM_EXC = (ValueError, _CrGE, _CrTE)
        _crown_drop_idx: dict = {}
        if _CROWN_ON:
            try:
                from auto_patch.crown import (build_crown_drop_field,
                                              emit_crown_spines)
                # Frozen VALUE CONTRACTS: seam pins (cross-tile terrain),
                # building seats, groundside mouth welds, seam spine
                # anchors.  Runway nodes are NOT frozen — they crown
                # through the field at their uniform per-ref drop.
                _crown_freeze = (
                    {i for i in building_seats if i < n}
                    | {i for i in _gs_hard if i < n}
                    | {i for i in _seam_pin_idx if i < n}
                    | {i for i, _cat in _hard_cat.items()
                       if _cat in ("seam_spine_anchor", "seat_on_spine",
                                   "gs_pin")})
                _crown_drop_idx = build_crown_drop_field(
                    layout, nodes, bucket_to_idx, _crown_freeze)
                # solve-time node registry: post-solve ring inserts are
                # recognised (and field-interpolated) against this set.
                layout._crown_solved_keys = set(bucket_to_idx)
            except _GEOM_EXC as _crown_exc:
                import O4_UI_Utils as _UIc
                _UIc.vprint(1, f"  [pav-builder] WARN: {icao}: crown "
                               f"field failed ({_crown_exc!r}) — flat "
                               f"sections emitted.")
                _crown_drop_idx = {}
        if _crown_drop_idx:
            _elev_emit = list(elev)
            for _i, _c in _crown_drop_idx.items():
                if _i < n:
                    _elev_emit[_i] = _elev_emit[_i] - _c
        else:
            _elev_emit = elev
        n_terms, n_rects, n_juncs = _writeback(layout, _elev_emit,
                                               bucket_to_idx)
        # Spine breaklines from the SOLVED route profiles (z′ ON the spine
        # equals z — spine nodes never crown) + the crowned runway pieces.
        if _CROWN_ON:
            try:
                _n_spine_ways = emit_crown_spines(
                    layout, nodes, bucket_to_idx, elev, _crown_drop_idx)
                if _n_spine_ways or _crown_drop_idx:
                    import O4_UI_Utils as _UIs
                    _UIs.vprint(1, f"  [pav-builder] {icao}: spine crown — "
                                   f"{len(_crown_drop_idx)} node(s) crowned, "
                                   f"{_n_spine_ways} spine breakline(s) "
                                   f"staged.")
            except _GEOM_EXC as _spine_exc:
                import O4_UI_Utils as _UIs2
                _UIs2.vprint(1, f"  [pav-builder] WARN: {icao}: spine "
                                f"breakline emission failed "
                                f"({_spine_exc!r}).")
        # SCOPED FINAL PROJECTION snapshot (user 2026-07-05): capture the
        # post-writeback state (per-canonical-node values as the projection
        # will re-read them + per-shape ring identities) so
        # ``final_grade_projection`` can prove which shapes nothing touched
        # and skip regenerating their law pairs.  Gate off → no snapshot →
        # the projection takes its full-rebuild path (byte-identical).
        # ``_fairing_moved_keys``/``_scoped_gate`` are bound iff the
        # global-slice branch above ran — the same condition
        # ``final_grade_projection`` requires, re-checked here.
        # BREAK-REGION export (user 2026-07-05, drive-to-zero): the solver's
        # broken quarantine = genuine anchor contradictions rendered as the
        # contained distance-weighted blend.  Persist their lat/lon so the
        # sidecar can tag them and the validator reports their over-cap
        # ramp pairs in a SEPARATE section — honest, never hidden, but not
        # mixed into the actionable within-shape count (SPLP seam pockets).
        # Service DEM-follow break blends join the export (user 2026-07-06,
        # handover fix (b)): contradictory welded anchors through a road
        # node render the designed blend — same quarantine semantics as
        # every other solver-declared pocket.
        _solve_broken_idx |= {
            i for i in (getattr(layout, "_service_break_idx", None) or ())
            if i < len(nodes)}
        layout._break_node_ll = [
            layout.m_to_ll(nodes[i][0], nodes[i][1])
            for i in sorted(_solve_broken_idx) if i < len(nodes)]
        if ((CURVE_NATIVE_SPINE or ROUTE_ARC_SPINE) and _scoped_gate):
            _solve_broken_keys = {key for key, i in bucket_to_idx.items()
                                  if i in _solve_broken_idx}
            _capture_projection_snapshot(layout, _fairing_moved_keys,
                                         _solve_broken_keys)
        if _os.environ.get("O4_STEP_DEBUG") == "1":
            print(f"  [unified] {icao}: {len(frozen)} spine node(s) solved, "
                  f"{n_free} body node(s); feasibility-project → {rem} edge(s) "
                  f"over cap ({bh} both-hard = genuine).")
            _lazy_certified = sum(1 for _sc in shape_constraints
                                  if _sc.get("lazy_certified"))
            if _lazy_certified:
                _still_lazy = sum(1 for _sc in shape_constraints
                                  if "lazy_expand" in _sc)
                print(f"  [flat-lazy] {icao}: {_lazy_certified} certified, "
                      f"{_lazy_certified - _still_lazy} expanded during the "
                      f"solve, {_still_lazy} never expanded")
        _report(icao, n_free, n_free, _time.time() - t0,
                n_terms, n_rects, n_juncs)
        return


# ── SCOPED FINAL PROJECTION (user 2026-07-05, O4_SCOPED_FINAL_PROJECTION) ────
# Shapely-domain exceptions only (project rule: never catch built-ins here).
def _snapshot_geom_exceptions():
    from shapely.errors import GEOSException, TopologicalError
    return (ValueError, GEOSException, TopologicalError)


def _canonical_ring_key(coords):
    """Rotation- and reflection-invariant identity of a ring's mm-rounded
    geometry — the ``geom_guard._canonical_ring`` comparison (same 3-decimal
    rounding, same invariances) in O(n log n) instead of that helper's
    O(n²) minimal-rotation scan: a simple closed ring IS its undirected
    edge multiset over its vertex cycle, so ``(vertex_count, sorted
    undirected edges)`` changes exactly when a vertex is moved / inserted /
    dropped and never on a ring rotation or direction flip."""
    pts = [(round(x, 3), round(y, 3)) for (x, y) in coords]
    if pts and pts[0] == pts[-1]:
        pts = pts[:-1]
    count = len(pts)
    if count == 0:
        return (0, ())
    edges = tuple(sorted(
        (pts[k], pts[(k + 1) % count])
        if pts[k] <= pts[(k + 1) % count]
        else (pts[(k + 1) % count], pts[k])
        for k in range(count)))
    return (count, edges)


# Roles whose ring geometry feeds the final projection's law graph or the
# grade context's classification inputs (building keys, rect-cap inheritance,
# road-carve / route-contact zones).  ``service_road`` is NOT a pavement role
# but its geometry builds the road-carve zone, so it must be snapshotted too.
def _snapshot_roles():
    from auto_patch.elevation_per_surface.solver_primitives import (
        PAVEMENT_ROLES)
    return set(PAVEMENT_ROLES) | {"service_road"}


# Roles whose geometry feeds a BUFFERED point-membership zone in the grade
# context (``build_context``): road-carve zone (service_road/service_junction,
# buffer ROAD_FRONTAGE_TOL_M) and route-contact zone (taxi-route pavements,
# buffer _ROUTE_CONTACT_TOL_M).  A change to one of these rings can flip the
# zone membership — and therefore the law budget — of an apron/junction node
# that sits NEAR the changed geometry without sharing a vertex with it.
_ZONE_ROLES = frozenset({
    "service_road", "service_junction",
    "junction", "primary_parallel", "secondary_parallel",
    "stub", "cross_connector",
})


def _capture_projection_snapshot(layout, fairing_moved_keys=None,
                                 broken_keys=None):
    """Record the post-writeback state ``final_grade_projection`` scopes
    against (user 2026-07-05):

    * ``values`` — per-canonical-node elevation EXACTLY as the projection
      will re-read it (same ``_build_node_list`` + ``_seed_elevations`` pair,
      no dem — identical readback semantics), keyed by canonical point key.
    * ``rings`` — multiset of ``(role, canonical_ring_key)`` for every shape
      whose geometry feeds the projection graph or the law context (see
      ``_snapshot_roles``); ring identity via ``_canonical_ring_key``
      (mm-rounded, rotation/reflection-invariant — the geom_guard comparison
      in O(n log n)).
    * ``fairing_moved`` — canonical keys the solve's post-projection edge
      fairing moved (their pairs were NOT re-enforced afterwards, so the
      "unchanged ⇒ already enforced" proof excludes them).
    * ``broken`` — canonical keys of the solve's BROKEN quarantine (genuine
      anchor contradictions, blended + immovable — detected on the FULL
      graph).  The scoped projection re-quarantines the unchanged ones so
      its sparser envelope cannot un-quarantine an infeasible pocket.

    ``_seed_elevations`` republishes ``layout._seam_pin_idx``/``_seam_pin_ll``
    in ITS node-index space; the solve's published sets are restored so
    downstream passes see exactly the state they see with the gate off."""
    from auto_patch.elevation_per_surface.solver_primitives import (
        _build_node_list, _seed_elevations)

    geom_exc = _snapshot_geom_exceptions()
    saved_pins = [(attr, getattr(layout, attr, None))
                  for attr in ("_seam_pin_idx", "_seam_pin_ll")]
    values: dict = {}
    try:
        nodes, bucket_to_idx = _build_node_list(layout)
        if nodes:
            elev, _is_hard, _have = _seed_elevations(layout, nodes,
                                                     bucket_to_idx)
            for key, idx in bucket_to_idx.items():
                values[key] = elev[idx]
    finally:
        for attr, saved in saved_pins:
            if saved is not None:
                setattr(layout, attr, saved)
            elif hasattr(layout, attr):
                delattr(layout, attr)

    rings: dict = {}
    roles = _snapshot_roles()
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
            continue
        if not (s.role in roles or getattr(s, "is_rect_cap", False)):
            continue
        if s.polygon.geom_type != "Polygon":
            continue        # never matches at projection time → stays changed
        try:
            ring_key = (s.role,
                        _canonical_ring_key(s.polygon.exterior.coords))
        except geom_exc:
            continue
        rings[ring_key] = rings.get(ring_key, 0) + 1

    layout._final_projection_snapshot = {  # type: ignore[attr-defined]
        "values": values,
        "rings": rings,
        "fairing_moved": set(fairing_moved_keys or ()),
        "broken": set(broken_keys or ()),
    }


def _scoped_projection_defer_ids(layout, nodes, bucket_to_idx, elev,
                                 snapshot):
    """The apron/junction shapes ``final_grade_projection`` may DEFER (pure
    lazy stubs): proven untouched since the solve's writeback.  Returns
    ``(defer_shape_ids, pre_broken_idx)`` — the deferrable shapes (by
    ``id(shape)``) and the solve's broken-quarantine nodes still at their
    blended values (to re-quarantine in the projection).

    A shape is deferrable only when ALL of:
      1. its ring geometry is unchanged (same ``(role, canonical_ring)``
         present in the snapshot, count-aware — the geom_guard comparison);
      2. none of its node VALUES changed (the projection's own seed vs the
         snapshot values, bitwise — same readback path both times);
      3. no node was moved by the solve's edge fairing (pairs not re-enforced
         after that pass);
      4. no law-context input touching it changed:
           * a node shared with ANY geometry-changed/new shape (building-key
             gains, rect-cap inheritance, shared-vertex writes) — marked via
             the changed shapes' current rings;
           * a node at a coordinate a VANISHED ring used to hold (building-
             key/rect-cap LOSSES — old geometry known only mm-rounded, exact
             coordinate match);
           * a node inside the BUFFERED dirty region of changed zone-role
             geometry (road-carve / route-contact membership flips reach
             ``max(ROAD_FRONTAGE_TOL_M, _ROUTE_CONTACT_TOL_M)`` beyond the
             changed ring, old and new).
    Everything the solve's final joint projection enforced on identical
    rings, values and budgets is provably still satisfied — deferring it
    skips regeneration; any node the projection later moves expands the
    shape's full pair set through the lazy machinery (tolerance 0)."""
    from auto_patch.layout import ROLE_APRON, ROLE_JUNCTION

    geom_exc = _snapshot_geom_exceptions()
    cps = layout.canonical_points
    snap_values = snapshot["values"]
    snap_rings = snapshot["rings"]

    # (2) + (3): value drift and fairing-moved nodes.  ``contaminated``
    # collects every changed/contaminated node EXCEPT the broken quarantine
    # (tracked separately: broken nodes contaminate deferral but only the
    # UNcontaminated ones re-quarantine).
    contaminated: set = set()
    for key, i in bucket_to_idx.items():
        previous = snap_values.get(key)
        if previous is None or previous != elev[i]:
            contaminated.add(i)
    for key in snapshot.get("fairing_moved", ()):
        i = bucket_to_idx.get(key)
        if i is not None:
            contaminated.add(i)

    # Solve-broken quarantine: an untouched broken node keeps the solve's
    # blend and stays immovable (``pre_broken`` — the sparser scoped
    # envelope may not re-detect the contradiction; un-quarantined pockets
    # grind the worklist and smear, measured CYXY 66 k → 11.5 M visits).  A
    # touched (value-changed or geometry-contaminated) broken node re-solves
    # normally.  Either way NOTHING touching a pocket defers — the pocket's
    # pairs must be generated, tallied and blended exactly like the full
    # rebuild's.
    broken_idx: set = set()
    for key in snapshot.get("broken", ()):
        i = bucket_to_idx.get(key)
        if i is not None:
            broken_idx.add(i)

    # (1): ring-identity comparison, count-aware.
    current = []        # (shape, ring_key | None, open_coords | None)
    ring_count: dict = {}
    roles = _snapshot_roles()
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
            continue
        if not (s.role in roles or getattr(s, "is_rect_cap", False)):
            continue
        ring_key = None
        coords = None
        if s.polygon.geom_type == "Polygon":
            try:
                coords = _open4(s.polygon)
                ring_key = (s.role,
                            _canonical_ring_key(s.polygon.exterior.coords))
            except geom_exc:
                ring_key = None
        current.append((s, ring_key, coords))
        if ring_key is not None:
            ring_count[ring_key] = ring_count.get(ring_key, 0) + 1

    geom_changed_ids: set = set()
    for (s, ring_key, _coords) in current:
        if ring_key is None \
                or snap_rings.get(ring_key, 0) < ring_count[ring_key]:
            geom_changed_ids.add(id(s))

    # Rings that VANISHED since the snapshot (mutated in place or removed).
    old_missing = [ring_key for ring_key, old_count in snap_rings.items()
                   if ring_count.get(ring_key, 0) < old_count]

    # (4a): nodes of geometry-changed shapes contaminate.
    for (s, _ring_key, coords) in current:
        if id(s) not in geom_changed_ids or not coords:
            continue
        for (x, y) in coords:
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if i is not None:
                contaminated.add(i)

    # (4b): exact-coordinate contamination from vanished rings (their
    # geometry survives only as the canonical edge multiset — mm-rounded).
    old_points: set = set()
    for (_role, (_count, canonical_edges)) in old_missing:
        for (point_a, point_b) in canonical_edges:
            old_points.add(point_a)
            old_points.add(point_b)
    if old_points:
        for i, (x, y) in enumerate(nodes):
            if (round(x, 3), round(y, 3)) in old_points:
                contaminated.add(i)

    # (4c): buffered dirty region of changed zone-role geometry (old + new):
    # every ring SEGMENT of a vanished/new zone-role ring, node membership by
    # vectorised dwithin query.
    zone_segments = []
    for (role, (_count, canonical_edges)) in old_missing:
        if role in _ZONE_ROLES:
            zone_segments.extend(canonical_edges)
    for (s, _ring_key, coords) in current:
        if (id(s) in geom_changed_ids and s.role in _ZONE_ROLES
                and coords and len(coords) >= 2):
            closed = list(coords) + [coords[0]]
            zone_segments.extend(zip(closed, closed[1:]))
    if zone_segments:
        from shapely.geometry import LineString, Point
        from shapely.strtree import STRtree
        from auto_patch.config import ROAD_FRONTAGE_TOL_M
        from auto_patch.grade_graph import _ROUTE_CONTACT_TOL_M
        zone_tol = max(ROAD_FRONTAGE_TOL_M, _ROUTE_CONTACT_TOL_M) + 0.01
        segment_tree = STRtree(
            [LineString(seg) for seg in zone_segments])
        node_points = [Point(x, y) for (x, y) in nodes]
        near_pairs = segment_tree.query(node_points, predicate="dwithin",
                                        distance=zone_tol)
        for node_index in near_pairs[0]:
            contaminated.add(int(node_index))

    pre_broken = broken_idx - contaminated
    changed_idx = contaminated | broken_idx

    if _os.environ.get("O4_STEP_DEBUG") == "1":
        _n_value = sum(1 for key, i in bucket_to_idx.items()
                       if snap_values.get(key) is None
                       or snap_values.get(key) != elev[i])
        _n_fair = sum(1 for key in snapshot.get("fairing_moved", ())
                      if key in bucket_to_idx)
        print(f"    [scoped-scope] nodes={len(nodes)} value_changed={_n_value} "
              f"fairing_moved={_n_fair} broken={len(broken_idx)} "
              f"geom_changed_shapes={len(geom_changed_ids)} "
              f"vanished_rings={len(old_missing)} "
              f"zone_segments={len(zone_segments)} "
              f"contaminated_total={len(changed_idx)}")

    # Deferrable = unchanged apron/junction with no changed node.
    defer_ids: set = set()
    for (s, _ring_key, coords) in current:
        if s.role not in (ROLE_APRON, ROLE_JUNCTION):
            continue
        if id(s) in geom_changed_ids or not coords:
            continue
        touched = False
        for (x, y) in coords:
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if i is None or i in changed_idx:
                touched = True
                break
        if not touched:
            defer_ids.add(id(s))
    return defer_ids, pre_broken


def _project_triangle_planes(layout, bucket_to_idx, elev, immovable,
                             joint, n):
    """Clamp each 3-vertex sloped shape's PLANE gradient to its role cap.

    A triangle renders as one plane; its gradient can exceed the role cap
    while every vertex pair stays inside the pairwise rounding envelope
    (``check_grade._check_plane_gradient``).  For each triangle over cap,
    move ONE free vertex the minimal amount that brings the plane inside
    the cap, clamped into the interval that vertex's own law edges allow
    (margined like the projection).  Returns ``(n_fixed, anchored_idx,
    broken_idx)`` — anchored vertices must not be re-perturbed by later
    passes; broken = no free vertex could lawfully fix the plane (the
    caller quarantines them)."""
    import math as _math
    from auto_patch.config import ROLE_GRADE_LIMITS
    from .one_solve import (_build_adjacency, _emit_quantization_margin,
                            _margined_budget)

    adjacency = _build_adjacency(joint, n)
    quant_margin = _emit_quantization_margin()
    cps = layout.canonical_points
    n_fixed = 0
    anchored: set = set()
    broken: set = set()

    def _gradient(pts, zs):
        (x1, y1), (x2, y2), (x3, y3) = pts
        z1, z2, z3 = zs
        nz = (x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)
        if abs(nz) < 1e-6:
            return None
        nx = (y2 - y1) * (z3 - z1) - (z2 - z1) * (y3 - y1)
        ny = (z2 - z1) * (x3 - x1) - (x2 - x1) * (z3 - z1)
        return (-nx / nz, -ny / nz)

    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty \
                or s.polygon.geom_type != "Polygon":
            continue
        cap = ROLE_GRADE_LIMITS.get(s.role)
        if not cap:
            continue
        try:
            ring = list(s.polygon.exterior.coords)
        except Exception:
            continue
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) != 3:
            continue
        idxs = [bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
                for (x, y) in ring]
        if any(i is None or i >= n for i in idxs):
            continue
        zs = [elev[i] for i in idxs]
        g = _gradient(ring, zs)
        if g is None:
            continue
        if _math.hypot(*g) <= cap:
            continue
        # Try each free vertex; pick the smallest lawful move.
        best = None                    # (move_size, vertex_pos, new_value)
        for k in range(3):
            i_move = idxs[k]
            if i_move in immovable:
                continue
            others = [(ring[m], zs[m]) for m in range(3) if m != k]
            pts = [others[0][0], others[1][0], ring[k]]
            fixed_z = [others[0][1], others[1][1]]
            g0 = _gradient(pts, fixed_z + [0.0])
            g1 = _gradient(pts, fixed_z + [1.0])
            if g0 is None or g1 is None:
                continue
            bx, by = g1[0] - g0[0], g1[1] - g0[1]
            a = bx * bx + by * by
            if a < 1e-18:
                continue               # gradient insensitive to this vertex
            b = 2.0 * (g0[0] * bx + g0[1] * by)
            c = g0[0] * g0[0] + g0[1] * g0[1] - cap * cap
            disc = b * b - 4.0 * a * c
            if disc < 0.0:
                continue               # no value of this vertex fixes it
            sq = _math.sqrt(disc)
            t_lo, t_hi = (-b - sq) / (2.0 * a), (-b + sq) / (2.0 * a)
            # law-edge interval for the moved vertex (margined budgets)
            lo_b, hi_b = -float("inf"), float("inf")
            for (other, budget) in adjacency.get(i_move, ()):
                if other >= n:
                    continue
                m_budget = _margined_budget(budget, quant_margin)
                lo_b = max(lo_b, elev[other] - m_budget)
                hi_b = min(hi_b, elev[other] + m_budget)
            lo = max(t_lo, lo_b)
            hi = min(t_hi, hi_b)
            if lo > hi:
                continue               # law edges forbid the fix
            cur = zs[k]
            new_val = min(max(cur, lo), hi)
            move = abs(new_val - cur)
            if best is None or move < best[0]:
                best = (move, k, new_val)
        if best is None:
            broken.update(i for i in idxs if i is not None)
            continue
        _move, k, new_val = best
        elev[idxs[k]] = new_val
        anchored.update(i for i in idxs if i is not None)
        n_fixed += 1
    return n_fixed, anchored, broken


def final_grade_projection(layout, icao: str = "", dem=None,
                           tile_lat: int = 0, tile_lon: int = 0) -> None:
    """LAST-WORD grade projection on the FINAL emitted geometry (round 4,
    user 2026-07-03).

    Post-solve passes (planarize inserts, final T-vertex weld adoptions,
    merges, clip rebuilds) reshape rings AFTER the elevation solve, so the
    law pairs of the FINAL rings are a superset of what the solve projected
    — measured at SPJC: 13 whole long chords + 51 inserted endpoints, most
    of the residual law-true violations.  Rebuild the law graph on the
    final shapes and run one scalar GS projection: runway/CIFP corners,
    tile-seam nodes and nodes welded to already-emitted FEATURE shapes
    (boundary ribbon / bridges / clearance adopted pavement values earlier)
    are HARD; building pads move as rigid flat groups; everything else
    flexes minimally from its solved value (warm seed → only violated
    neighbourhoods move).  Gate ``O4_FINAL_GRADE_PROJECTION=0`` disables.
    Global-slice only (the rect path keeps its byte-identical emit)."""
    from auto_patch.config import CURVE_NATIVE_SPINE, ROUTE_ARC_SPINE
    if not (CURVE_NATIVE_SPINE or ROUTE_ARC_SPINE):
        return
    # DEFAULT ON (2026-07-04): the 2026-07-03 "no change at SPJC"
    # measurement predated the EXACT-AXES sidecar — the residuals then
    # were reader-divergent pairs no projection could fix.  With unified
    # readers this pass closes exactly the post-solve mutation classes
    # (planarize/T-weld inserts, clip rebuilds, service DEM-follow noise):
    # CYXY within-shape 299 → 97, worst 8.35 % → 6.07 % (one rounding
    # pair).  Costs ~12-15 s.  ``O4_FINAL_GRADE_PROJECTION=0`` restores
    # the previous behaviour.
    if _os.environ.get("O4_FINAL_GRADE_PROJECTION", "1") != "1":
        return
    from auto_patch.elevation_per_surface.solver_primitives import (
        PAVEMENT_ROLES, _build_node_list, _build_shape_constraints,
        _runway_node_set, _seed_elevations, _writeback)
    from auto_patch import grade_graph as _GG
    from auto_patch.layout import ROLE_BUILDING
    from .one_solve import feasibility_project

    t0 = _time.time()
    _stage_t = {}
    _stage_prev = [t0]

    def _stage(name):
        now = _time.time()
        _stage_t[name] = _stage_t.get(name, 0.0) + (now - _stage_prev[0])
        _stage_prev[0] = now

    nodes, b2i = _build_node_list(layout)
    if not nodes:
        return
    elev, base_hard, _have = _seed_elevations(layout, nodes, b2i)
    n = len(elev)
    _stage("seed")

    ctx = _GG.build_context(layout, b2i)
    _stage("ctx")
    runway_idx = _runway_node_set(layout, b2i)
    # SCOPED PROJECTION (user 2026-07-05, gate ``O4_SCOPED_FINAL_PROJECTION``
    # default ON; off = full rebuild): a shape needs re-projection only if
    # its ring geometry changed after the solve, any of its node values
    # changed after the solve's writeback, or a law-context input touching it
    # changed (see ``_scoped_projection_defer_ids``).  Everything else was
    # already projected during the solve on identical rings/values — provably
    # nothing to do; deferred shapes become zero-cost lazy stubs that expand
    # the moment the projection moves one of their nodes.
    snapshot = getattr(layout, "_final_projection_snapshot", None)
    scoped = (snapshot is not None and _os.environ.get(
        "O4_SCOPED_FINAL_PROJECTION", "1") == "1")
    defer_ids: set = set()
    pre_broken: set = set()
    if scoped:
        try:
            defer_ids, pre_broken = _scoped_projection_defer_ids(
                layout, nodes, b2i, elev, snapshot)
        except _snapshot_geom_exceptions():
            defer_ids = set()
            pre_broken = set()
            scoped = False        # geometry hiccup → sound full rebuild
    _stage("scope")
    # ── SPINE CROWN v2 transform (part 30): this projection enforces the
    # law, and the law lives in UNCROWNED space z′ = z + c (the emitted
    # crown is a designed sub-cap offset — see the solve's writeback).
    # Post-solve ring inserts (planarize / T-welds) carry VALUES linearly
    # interpolated along their ring edge, so they join the field at the
    # SAME interpolation of the flanking solve-time drops first
    # (crown.extend_field_to_new_ring_nodes) — value and field stay
    # consistent, and the sidecar export the validator reads is complete.
    # Then: add c, project, subtract before writeback.
    _crown_by_key = getattr(layout, "_crown_drop_key", None) or {}
    _crown_of: dict = {}
    if _crown_by_key:
        try:
            from auto_patch.crown import extend_field_to_new_ring_nodes
            extend_field_to_new_ring_nodes(layout, b2i)
        except _snapshot_geom_exceptions():
            pass
        _crown_by_key = getattr(layout, "_crown_drop_key", None) or {}
        for _key, _i in b2i.items():
            _v = _crown_by_key.get(_key)
            if _v:
                _crown_of[_i] = _v
                elev[_i] = elev[_i] + _v
    if scoped:
        shape_constraints = _build_shape_constraints(
            layout, b2i, ctx=ctx, defer_shape_ids=defer_ids)
        for _entry in shape_constraints:
            if _entry.get("lazy_scoped"):
                _entry["lazy_seed"] = [elev[i] for i in _entry["lazy_nodes"]]
        _stage("constraints")
        G = _GG.build_unified_graph(layout, b2i, ctx=ctx,
                                    skip_edge_shape_ids=defer_ids,
                                    include_spine=False)
    else:
        # FLATNESS-CERTIFIED LAZY TIER (user 2026-07-05): here ``elev`` is the
        # SOLVED surface (warm seed), so a certified shape stays lazy only when
        # the whole pipeline left every one of its nodes exactly at the DEM seed
        # (bitwise — the entry check compares against the same sampler's values);
        # anything the solve touched expands at projection entry.  ``dem`` comes
        # from the pipeline caller (same tile frame as the elevation solve).
        _hard_for_certificate = ({i for i in range(n) if base_hard[i]}
                                 | {i for i in runway_idx if i < n})
        shape_constraints = _build_shape_constraints(
            layout, b2i, ctx=ctx, dem=dem, tile_lat=tile_lat,
            tile_lon=tile_lon, hard_nodes=_hard_for_certificate)
        _stage("constraints")
        G = _GG.build_unified_graph(layout, b2i, ctx=ctx)
    u_edges = [(a, b, cap.at(_GG._dist(G.pos.get(a), G.pos.get(b)), 0.0))
               for (a, b, cap, _sp) in G.edges
               if a in G.pos and b in G.pos]
    joint = list(shape_constraints) + [{"edges": u_edges}]
    _stage("graph")

    hard = {i for i in range(n) if base_hard[i]}
    hard |= {i for i in runway_idx if i < n}
    # tile-seam nodes: terrain-pinned for cross-tile stitching.
    # ``terrain_hard`` tracks the TERRAIN-dictated subset of the hard set
    # (seam pins + agreeing feature welds below): a violated law edge
    # between two terrain-pinned nodes is the terrain's own slope, not a
    # solver miss — exported to the break quarantine after the projection.
    terrain_hard: set = set()
    try:
        for i, (x, y) in enumerate(nodes):
            la, lo = layout.m_to_ll(x, y)
            if (abs(la - round(la)) < 1e-7 or abs(lo - round(lo)) < 1e-7):
                hard.add(i)
                terrain_hard.add(i)
    except Exception:
        pass
    # nodes welded to already-emitted FEATURE shapes (ribbon/bridge/
    # clearance/groundside copied pavement values BEFORE this pass — moving
    # the pavement side now would tear those welds open).
    #
    # AGREEMENT GATE (user 2026-07-06, HECA service-road cliffs): a weld is
    # only a weld when the two sides AGREE — the feature adopted the
    # pavement's value.  A coincident vertex whose values already DISAGREE
    # is a torn weld or a mere contact (HECA: road nodes the solve's
    # envelope had lifted 3.4 m above their ring sat mm-coincident with
    # raw-DEM groundside verts 5 m below; hardening froze the damage and
    # the projection reported the resulting walls as 16 "genuine" both-hard
    # edges).  Freezing preserves nothing there — leave the pavement node
    # FREE so the projection solves it lawfully.  A feature vertex whose
    # altitude cannot be derived keeps the conservative hardening.
    feat_alt_by_key: dict = {}
    for s in layout.shapes:
        if (s.role in PAVEMENT_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        try:
            ring = list(s.polygon.exterior.coords)
            per_vertex = None
            if s.node_altitudes and len(s.node_altitudes) >= len(ring) - 1:
                per_vertex = [float(a) for a in
                              s.node_altitudes[:len(ring)]]
            elif s.altitude is not None:
                per_vertex = [float(s.altitude)] * len(ring)
            for k, (x, y) in enumerate(ring):
                key = (round(x, 3), round(y, 3))
                value = (per_vertex[k] if per_vertex is not None
                         and k < len(per_vertex) else None)
                if key in feat_alt_by_key and feat_alt_by_key[key] is None:
                    continue          # an unverifiable weld stays hard
                if value is None:
                    feat_alt_by_key[key] = None
                elif key not in feat_alt_by_key:
                    feat_alt_by_key[key] = value
        except Exception:
            continue
    _WELD_AGREE_TOL_M = 0.05
    # WELD-KEY tolerance (user 2026-07-06, CYXY apron #29): a feature
    # contact vertex inserted post-solve (boundary-bridge insert) can sit
    # a few mm off the pavement ring vertex it welds — mm-exact keys miss
    # it and the node wrongly stays free.  Match at the canonical 0.5 m
    # registry tolerance via a coarse grid.
    _WELD_KEY_TOL_M = 0.5
    torn_feature_weld: set = set()
    feat_grid: dict = {}
    for (_kx, _ky), _v in feat_alt_by_key.items():
        feat_grid.setdefault((int(_kx // _WELD_KEY_TOL_M),
                              int(_ky // _WELD_KEY_TOL_M)),
                             []).append((_kx, _ky, _v))
    if feat_alt_by_key:
        for i, (x, y) in enumerate(nodes):
            feature_value = feat_alt_by_key.get((round(x, 3), round(y, 3)),
                                                "absent")
            if feature_value == "absent":
                _cx = int(x // _WELD_KEY_TOL_M)
                _cy = int(y // _WELD_KEY_TOL_M)
                _best = None
                for _ox in (-1, 0, 1):
                    for _oy in (-1, 0, 1):
                        for (_fx, _fy, _fv) in feat_grid.get(
                                (_cx + _ox, _cy + _oy), ()):
                            _fd = ((x - _fx) ** 2 + (y - _fy) ** 2) ** 0.5
                            if _fd <= _WELD_KEY_TOL_M and (
                                    _best is None or _fd < _best[0]):
                                _best = (_fd, _fv)
                if _best is None:
                    continue
                feature_value = _best[1]
            # crown transform: elev is in z′ space here — lift the
            # feature's z value by the node's crown drop before comparing.
            if (feature_value is None
                    or abs(feature_value + _crown_of.get(i, 0.0) - elev[i])
                    <= _WELD_AGREE_TOL_M):
                hard.add(i)
                terrain_hard.add(i)
            else:
                # TORN WELD: the feature holds a different value than
                # the pavement at the same coordinate — the emit
                # consensus will merge the two nodes toward the feature
                # side, so pairs through this vertex render the
                # feature's terrain value regardless of what the
                # projection solves (CYXY apron #29: bridge 692.85 vs
                # pavement 693.00 emitted the bridge value).  Treated as
                # terrain-dictated for the quarantine scan below.
                torn_feature_weld.add(i)

    # building pads: rigid movable FLAT groups (same model as the yield).
    cps = layout.canonical_points
    pad_groups = []
    pad_nodes: set = set()
    if _os.environ.get("O4_YIELD_MOVABLE_PADS", "1") == "1":
        for s in layout.shapes:
            if (s.role != ROLE_BUILDING or s.polygon is None
                    or s.polygon.is_empty):
                continue
            g = {b2i.get(cps.get_or_add(float(x), float(y)))
                 for (x, y) in s.polygon.exterior.coords}
            g = {i for i in g if i is not None and i < n}
            if len(g) >= 2:
                pad_groups.append(g)
                pad_nodes |= g
        hard -= pad_nodes

    _stage("hard")
    # Capture the pockets THIS projection declares broken: they are excluded
    # from the sweeps by design (contained blend), so they must join the
    # sidecar's break_nodes export below or the validator reports their
    # over-cap ramps as ACTIONABLE (the solve-time export alone missed any
    # pocket only the final geometry manufactures).
    _projection_broken_idx: set = set()
    rem, bh = feasibility_project(elev, joint, hard, force_scalar=True,
                                  max_iters=400,
                                  flat_groups=pad_groups or None,
                                  pre_broken=(pre_broken or None),
                                  broken_out=_projection_broken_idx)
    # TERRAIN-PINNED PAIR EXPORT (user 2026-07-06, CYXY #26/#29 after the
    # apron route-proximity cut): a violated law edge touching a
    # terrain-dictated pin (tile-seam node, agreeing boundary/feature
    # weld) after the projection has CONVERGED is the terrain's own slope
    # winning over the shape law — the free end, had it any slack, would
    # already have moved (a hillside strip welded to the boundary ribbon
    # carries the terrain's 1.25 % between ground-truth pins no 1 % law
    # can beat).  Export those endpoints to the break quarantine.  Scoped
    # to TERRAIN pins only (part-18 ruling: blanket both-hard quarantine
    # hides real anchor bugs — runway/pad/weld classes stay actionable).
    _VIOL_TOL_M = 0.03
    _terrain_like = terrain_hard | torn_feature_weld
    if _terrain_like:
        for _sc in joint:
            for _e in _sc["edges"]:
                _a, _b, _bud = _e[0], _e[1], _e[2]
                if (_a >= n or _b >= n
                        or (_a not in _terrain_like
                            and _b not in _terrain_like)):
                    continue
                if abs(elev[_a] - elev[_b]) > _bud + _VIOL_TOL_M:
                    _projection_broken_idx.add(_a)
                    _projection_broken_idx.add(_b)
        # DEFERRED shapes carry no edges in ``joint`` (the scoped
        # projection proved them untouched), so a terrain-pinned pair
        # inside one is invisible to the scan above (CYXY apron #29: a
        # hillside strip welded to the boundary ribbon, deferred, its
        # 57 m chord 0.25 % over the 1 % law).  Scan such rings directly:
        # every (terrain vertex ↔ ring vertex) chord that stays inside
        # the polygon at the shape's role cap.
        from auto_patch.config import ROLE_GRADE_LIMITS as _RGL
        from shapely.geometry import LineString as _LS
        _cps_t = layout.canonical_points
        for _s in layout.shapes:
            _cap_role = _RGL.get(_s.role)
            if (not _cap_role or _s.polygon is None or _s.polygon.is_empty
                    or _s.polygon.geom_type != "Polygon"):
                continue
            try:
                _ring_t = list(_s.polygon.exterior.coords)[:-1]
            except Exception:
                continue
            _idx_t = [b2i.get(_cps_t.get_or_add(float(x), float(y)))
                      for (x, y) in _ring_t]
            _terrain_on_ring = [(k, i) for k, i in enumerate(_idx_t)
                                if i is not None and i < n
                                and i in _terrain_like]
            if not _terrain_on_ring:
                continue
            _poly_buf = None
            for (_kt, _it) in _terrain_on_ring:
                for _ko, _io in enumerate(_idx_t):
                    if _io is None or _io >= n or _io == _it:
                        continue
                    _dx = _ring_t[_kt][0] - _ring_t[_ko][0]
                    _dy = _ring_t[_kt][1] - _ring_t[_ko][1]
                    _dd = (_dx * _dx + _dy * _dy) ** 0.5
                    if _dd < 0.5:
                        continue
                    if (abs(elev[_it] - elev[_io])
                            <= _cap_role * _dd + _VIOL_TOL_M):
                        continue
                    if _poly_buf is None:
                        try:
                            _poly_buf = _s.polygon.buffer(0.1)
                        except Exception:
                            break
                    try:
                        if not _poly_buf.covers(_LS(
                                [_ring_t[_kt], _ring_t[_ko]])):
                            continue      # chord leaves the shape
                    except Exception:
                        continue
                    _projection_broken_idx.add(_it)
                    _projection_broken_idx.add(_io)
        if _os.environ.get("O4_STEP_DEBUG") == "1":
            print(f"    [terrain-scan] terrain_hard={len(terrain_hard)} "
                  f"broken_now={len(_projection_broken_idx)}")
    _dbg_ll = _os.environ.get("O4_PROJ_DEBUG_LL")
    if _dbg_ll:
        try:
            for _part in _dbg_ll.split(";"):
                _dla, _dlo = (float(v) for v in _part.split(","))
                import math as _dbg_math
                _bi = min(range(len(nodes)), key=lambda i: _dbg_math.hypot(
                    (layout.m_to_ll(nodes[i][0], nodes[i][1])[0] - _dla)
                    * 111320,
                    (layout.m_to_ll(nodes[i][0], nodes[i][1])[1] - _dlo)
                    * 54460))
                _bla, _blo = layout.m_to_ll(nodes[_bi][0], nodes[_bi][1])
                _dd = _dbg_math.hypot((_bla - _dla) * 111320,
                                      (_blo - _dlo) * 54460)
                _n_edges = sum(
                    1 for _sc in joint for _e in _sc["edges"]
                    if _e[0] == _bi or _e[1] == _bi)
                print(f"    [proj-dbg] ({_dla},{_dlo}) -> idx={_bi} "
                      f"d={_dd:.2f}m elev={elev[_bi]:.2f} "
                      f"hard={_bi in hard} terrain={_bi in terrain_hard} "
                      f"pad={_bi in pad_nodes} joint_edges={_n_edges} "
                      f"broken={_bi in _projection_broken_idx}")
        except Exception as _e:
            print(f"    [proj-dbg] error {_e!r}")
    # TRIANGLE-PLANE LAW (user 2026-07-06): a 3-vertex sloped shape
    # renders as ONE plane, and can satisfy every vertex-PAIR budget yet
    # tilt beyond its role cap along the plane gradient (skinny slivers:
    # HECA junction #258 at the 05C corner 6.4 %, apron #41 by building7
    # 2.4 % — pairwise all inside the rounding envelope).  The pairwise
    # projection cannot see this, so clamp it here: move the triangle's
    # freest vertex the minimum that brings the plane inside the cap,
    # bounded by the interval its own law edges allow (ORDERING LAW —
    # post-projection moves are law-guarded).  Unfixable triangles join
    # the break quarantine below.  Fixed vertices are anchored through
    # the later edge fairing so nothing re-tilts them.
    _tri_anchor_idx: set = set()
    if _os.environ.get("O4_TRIANGLE_PLANE_LAW", "1") == "1":
        _n_tri_fixed, _tri_anchor_idx, _tri_broken = \
            _project_triangle_planes(layout, b2i, elev,
                                     hard | pad_nodes, joint, n)
        _projection_broken_idx |= _tri_broken
        if (_n_tri_fixed or _tri_broken) and _os.environ.get(
                "O4_STEP_DEBUG") == "1":
            print(f"    [triangle-plane] fixed {_n_tri_fixed}, "
                  f"quarantined {len(_tri_broken)} vertex(es)")
    _stage("project")
    if _projection_broken_idx:
        try:
            _existing_break_ll = list(
                getattr(layout, "_break_node_ll", None) or [])
            _seen_break = {(round(la, 7), round(lo, 7))
                           for (la, lo) in _existing_break_ll}
            for i in sorted(_projection_broken_idx):
                if i >= len(nodes):
                    continue
                la, lo = layout.m_to_ll(nodes[i][0], nodes[i][1])
                if (round(la, 7), round(lo, 7)) not in _seen_break:
                    _existing_break_ll.append((la, lo))
            layout._break_node_ll = _existing_break_ll
        except Exception:
            pass
    _n_deferred = _n_expanded = 0
    if scoped:
        _n_deferred = sum(1 for _sc in shape_constraints
                          if _sc.get("lazy_scoped"))
        _n_expanded = sum(1 for _sc in shape_constraints
                          if _sc.get("lazy_scoped")
                          and "lazy_expand" not in _sc)
        if _os.environ.get("O4_STEP_DEBUG") == "1":
            for _sc in shape_constraints:
                if not _sc.get("lazy_scoped"):
                    continue
                _first = _sc["nodes"][0] if _sc["nodes"] else None
                _at = (f"({nodes[_first][0]:.0f},{nodes[_first][1]:.0f})"
                       if _first is not None else "?")
                print(f"    [scoped] deferred {_sc['role']} "
                      f"ref={_sc['ref'] or '-'} area={_sc['area']:.0f} "
                      f"at={_at} "
                      f"{'EXPANDED' if 'lazy_expand' not in _sc else 'kept'}")
    if _os.environ.get("O4_STEP_DEBUG") == "1":
        _lazy_certified = sum(1 for _sc in shape_constraints
                              if _sc.get("lazy_certified"))
        if _lazy_certified:
            _still_lazy = sum(1 for _sc in shape_constraints
                              if "lazy_expand" in _sc)
            print(f"  [flat-lazy] {icao} final projection: "
                  f"{_lazy_certified} certified, "
                  f"{_lazy_certified - _still_lazy} expanded, "
                  f"{_still_lazy} never expanded")
    # Re-fair the ring edges the projection just perturbed (the GS
    # distributes a cap-grade climb as a sawtooth between alternate
    # nodes; a linear cap-grade profile satisfies the same pairs) —
    # same second-difference law as the solve-time pass.
    #
    # LAW-GUARDED (user 2026-07-05): this is the LAST pass before
    # writeback — nothing re-enforces the pairs it perturbs, and the
    # unguarded POCS moved junction ring nodes over their MESH-chord
    # budgets (pairs crossing between two ring runs, invisible to the
    # ring triples) by a median 1.8 cm — the SPJC 43-pair cm-noise
    # class.  Every move is now clamped into the interval its node's
    # law edges allow, at the same margined budgets the projection just
    # enforced (``_margined_budget``); nodes of shapes whose body pairs
    # are still lazy (never expanded ⇒ the projection proved them
    # untouched, so there is no sawtooth to re-fair there) are anchored.
    if _os.environ.get("O4_EDGE_FAIRING", "1") == "1":
        from auto_patch.config import TAXIWAY_MAX_GRADE_CHANGE_PER_M
        from .one_solve import (_build_adjacency, _emit_quantization_margin,
                                _margined_budget)
        _quant_margin = _emit_quantization_margin()
        _law_adjacency = {
            node: [(other, _margined_budget(budget, _quant_margin))
                   for (other, budget) in incident]
            for node, incident in _build_adjacency(joint, n).items()}
        _lazy_guard_nodes: set = set()
        for _sc in shape_constraints:
            if "lazy_expand" in _sc:
                for _node in (_sc.get("lazy_nodes")
                              or _sc.get("nodes") or ()):
                    if isinstance(_node, int):
                        _lazy_guard_nodes.add(_node)
        _fair_ring_edges(layout, elev, b2i,
                         hard | _lazy_guard_nodes | _tri_anchor_idx, None,
                         TAXIWAY_MAX_GRADE_CHANGE_PER_M,
                         law_adjacency=_law_adjacency)
    _stage("fairing")
    # crown transform back: z = z′ − c (see the entry transform above).
    if _crown_of:
        for _i, _v in _crown_of.items():
            elev[_i] = elev[_i] - _v
    _writeback(layout, elev, b2i)
    _stage("writeback")
    try:
        import O4_UI_Utils as _UI
        _scope_note = ""
        if scoped:
            _scope_note = (f" [scoped: {_n_deferred} deferred, "
                           f"{_n_expanded} expanded]")
        _UI.vprint(1, f"  [final-projection] {icao}: {len(nodes)} nodes, "
                      f"{len(hard)} hard, {len(pad_groups)} pad group(s) → "
                      f"{rem} edge(s) over cap ({bh} both-hard) "
                      f"in {_time.time() - t0:.1f}s.{_scope_note}")
        if _os.environ.get("O4_PROJ_TIMING") == "1":
            _UI.vprint(1, "  [final-projection-timing] " + " ".join(
                f"{name}={_stage_t.get(name, 0.0):.1f}s"
                for name in ("seed", "ctx", "scope", "constraints",
                             "graph", "hard", "project", "fairing",
                             "writeback")))
    except Exception:
        pass


def _open4(poly):
    c = list(poly.exterior.coords)
    return c[:-1] if c and c[0] == c[-1] else c


def _flatten_rect_ends(layout, bucket_to_idx, elev, base_hard, frozen_spine):
    """Make each sloping taxi rect a FLAT-ENDED tilted plane: both corners of each
    short end take that end's solved-spine elevation (mean over the corners the
    spine solve actually set), so the rect emits as a clean plane (the validator
    checks all-pair).  Freezes the corners.  Returns each rect's plane as
    ``(corner_idx_set, (e0x,e0y), e0_node, (e1x,e1y), e1_node)`` for the final cap
    re-stamp (the node indices let the cap re-read the rect's FINAL ends)."""
    import math
    from auto_patch.junction_rules import SLOPING_RECT_ROLES
    cps = layout.canonical_points
    n = len(elev)

    def _idx(x, y):
        return bucket_to_idx.get(cps.get_or_add(float(x), float(y)))

    from auto_patch.grade_graph import _rect_ends
    planes = []
    for s in layout.shapes:
        if (s.role not in SLOPING_RECT_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        coords = _open4(s.polygon)
        if len(coords) < 4:
            continue
        endA, endB, _ext = _rect_ends(coords)
        if endA is None:
            continue
        ends_mid, end_node, ckeys, ok = [], [], set(), True
        for grp in (endA, endB):
            cis = [_idx(*coords[k]) for k in grp]
            if any(c is None or c >= n for c in cis):
                ok = False
                break
            solved = [c for c in cis if c in frozen_spine]
            if not solved:
                ok = False
                break
            ez = sum(elev[c] for c in solved) / len(solved)
            for c in cis:
                elev[c] = ez            # flatten the whole end
                ckeys.add(c)
            mx = sum(coords[k][0] for k in grp) / len(grp)
            my = sum(coords[k][1] for k in grp) / len(grp)
            ends_mid.append((mx, my))
            end_node.append(cis[0])
        if ok and len(ends_mid) == 2:
            planes.append((ckeys, ends_mid[0], end_node[0],
                           ends_mid[1], end_node[1]))
    return planes


def _restamp_caps_unified(layout, bucket_to_idx, elev, rect_planes, frozen_spine):
    """Continue each end-cap as a planar extension of its parent rect's FINAL
    plane (matched by ≥2 shared corners), set LAST so nothing moves it.  A cap
    corner the SPINE owns (``frozen_spine`` — a junction node) is left untouched:
    the spine is paramount, the cap yields there.  Mutates ``elev`` in place."""
    cps = layout.canonical_points
    n = len(elev)

    def _idx(x, y):
        return bucket_to_idx.get(cps.get_or_add(float(x), float(y)))

    for s in layout.shapes:
        if (not getattr(s, "is_rect_cap", False) or s.polygon is None
                or s.polygon.is_empty):
            continue
        coords = _open4(s.polygon)
        if len(coords) < 3:
            continue
        ckeys = {_idx(x, y) for (x, y) in coords}
        best, best_sh = None, 1
        for pl in rect_planes:
            sh = len(pl[0] & ckeys)
            if sh > best_sh:
                best_sh, best = sh, pl
        if best is None:
            continue
        _ck, e0, e0n, e1, e1n = best
        z0 = elev[e0n] if e0n < n else None
        z1 = elev[e1n] if e1n < n else None
        if z0 is None or z1 is None:
            continue
        ax, ay = e1[0] - e0[0], e1[1] - e0[1]
        L2 = ax * ax + ay * ay
        if L2 < 1e-9:
            continue
        for (x, y) in coords:
            ci = _idx(x, y)
            if ci is None or ci >= n or ci in frozen_spine:
                continue
            t = ((x - e0[0]) * ax + (y - e0[1]) * ay) / L2
            elev[ci] = z0 + t * (z1 - z0)


def _seam_spine_anchors(layout, G, spine_adj, elev, base_hard,
                        dem, tile_lat, tile_lon, cut_lines):
    """Pin the nearest SPINE node to each taxi-centerline × tile-seam crossing at
    the SMOOTHED seam DEM (HARD), so ``_solve_spine_profile`` grades the route
    DOWN to the seam over the centerline length instead of leaving the spine at
    the plateau level (the SPLP tile-77 seam: spine stuck ~74.6, seam 72.2 → the
    apron body cliffed).  Returns the count pinned."""
    from shapely.geometry import Point          # noqa: F401  (geom predicates)
    from auto_patch.elevation import _sample_dem
    n = len(elev)
    spine_pts = [(i, G.pos[i]) for i in spine_adj if i in G.pos and i < n]
    if not spine_pts:
        return 0
    pinned = 0
    seen: set = set()
    for entry in (getattr(layout, "apt_taxi_centerlines", []) or []):
        ln = entry.line if hasattr(entry, "line") else (entry[0] if isinstance(entry, (tuple, list)) else entry)
        if ln is None or ln.is_empty:
            continue
        for cut in cut_lines:
            try:
                inter = ln.intersection(cut)
            except Exception:                              # pragma: no cover
                continue
            if inter.is_empty:
                continue
            pts = ([inter] if inter.geom_type == "Point"
                   else [g for g in getattr(inter, "geoms", [])
                         if g.geom_type == "Point"])
            for p in pts:
                bi, (bx, by) = min(
                    spine_pts,
                    key=lambda t: (t[1][0] - p.x) ** 2 + (t[1][1] - p.y) ** 2)
                if bi in seen or (bx - p.x) ** 2 + (by - p.y) ** 2 > 30.0 ** 2:
                    continue
                lat, lon = layout.m_to_ll(bx, by)
                v = _sample_dem(dem, tile_lat, tile_lon, lat, lon)
                if v is None or v != v:
                    continue
                elev[bi] = float(v)
                base_hard[bi] = True
                seen.add(bi)
                pinned += 1
    return pinned


def _fair_spine_chains(elev, spine_adj, anchors, node_band, nodes_xy,
                       k_rate, *, max_sweeps=400, tol=1e-4):
    """FAIRING (user 2026-07-04, task 3): bound the grade CHANGE between
    consecutive spine segments along every chain —
    ``|g2 − g1| ≤ k_rate·(L1 + L2)/2`` — the taxiway vertical-curve
    K-factor analog (``config.TAXIWAY_MAX_GRADE_CHANGE_PER_M``,
    tunable via ``O4_TAXIWAY_CURVE_RUN_M``).

    The grade law bounds only the FIRST derivative, so the spine solve
    tracks DEM noise in legal ±cap wiggles (the residual-waviness
    class); real grading is long linear/parabolic profiles.  This is a
    POCS pass on second-difference constraints: a too-sharp sag raises
    its centre vertex, a crest lowers it, split by segment stiffness
    (``δ/(1/L1 + 1/L2)``), clamped into the reach band.  Anchors
    (runway contacts, seam pins) never move — the curve fits BETWEEN
    them.  Chains are maximal degree-2 runs of the spine graph; the
    profile through a junction node (degree ≠ 2) is left to the
    junction's own solve.

    Mutates ``elev``; returns the number of triples still over the
    rate (honest residual — anchors can force a kink)."""
    import math
    deg = {i: len(lst) for i, lst in spine_adj.items()}
    visited_edges: set = set()
    chains = []
    for start, lst in spine_adj.items():
        if deg.get(start, 0) == 2:
            continue                       # chains start at break nodes
        for (j, _w) in lst:
            e = (start, j) if start < j else (j, start)
            if e in visited_edges:
                continue
            visited_edges.add(e)
            chain = [start, j]
            prev, cur = start, j
            while deg.get(cur, 0) == 2:
                nxt = [k for (k, _w2) in spine_adj[cur] if k != prev]
                if not nxt:
                    break
                nxt = nxt[0]
                e2 = (cur, nxt) if cur < nxt else (nxt, cur)
                if e2 in visited_edges:
                    break
                visited_edges.add(e2)
                chain.append(nxt)
                prev, cur = cur, nxt
            if len(chain) >= 3:
                chains.append(chain)
    if not chains:
        return 0

    def _seg_len(a, b):
        (xa, ya), (xb, yb) = nodes_xy[a], nodes_xy[b]
        return math.hypot(xa - xb, ya - yb)

    chain_lens = [[_seg_len(c[k], c[k + 1]) for k in range(len(c) - 1)]
                  for c in chains]
    n_band = len(node_band) if node_band is not None else 0
    for _sweep in range(max_sweeps):
        worst_move = 0.0
        for c, lens in zip(chains, chain_lens):
            for t in range(1, len(c) - 1):
                b = c[t]
                if b in anchors:
                    continue
                l1 = lens[t - 1]
                l2 = lens[t]
                if l1 < 0.5 or l2 < 0.5:
                    continue
                a, d = c[t - 1], c[t + 1]
                g1 = (elev[b] - elev[a]) / l1
                g2 = (elev[d] - elev[b]) / l2
                dg = g2 - g1
                lim = k_rate * 0.5 * (l1 + l2)
                ex = abs(dg) - lim
                if ex <= 1e-6:
                    continue
                delta = math.copysign(ex, dg) / (1.0 / l1 + 1.0 / l2)
                nb = elev[b] + delta
                band = node_band[b] if b < n_band else None
                if band is not None:
                    lo, hi = band
                    if lo <= hi:
                        nb = min(max(nb, lo), hi)
                moved = abs(nb - elev[b])
                if moved:
                    elev[b] = nb
                    if moved > worst_move:
                        worst_move = moved
        if worst_move < tol:
            break
    # honest residual count (anchor- or band-forced kinks)
    n_over = 0
    for c, lens in zip(chains, chain_lens):
        for t in range(1, len(c) - 1):
            l1, l2 = lens[t - 1], lens[t]
            if l1 < 0.5 or l2 < 0.5:
                continue
            g1 = (elev[c[t]] - elev[c[t - 1]]) / l1
            g2 = (elev[c[t + 1]] - elev[c[t]]) / l2
            if abs(g2 - g1) - k_rate * 0.5 * (l1 + l2) > 1e-4:
                n_over += 1
    return n_over


def _fair_ring_edges(layout, elev, bucket_to_idx, anchors, node_band,
                     k_rate, *, max_bend_deg=25.0, min_seg_m=3.0,
                     max_sweeps=200, tol=1e-4, law_adjacency=None):
    """Second-difference fairing on STRAIGHT airside boundary runs (user
    2026-07-04, CYXY taxiway E edge): the ``_fair_spine_chains`` law
    covers spine chains only, so a corridor's ring EDGE still tracks DEM
    noise in legal ±cap sawtooth (±0.8 % grade alternation every 12 m).
    Same POCS: a too-sharp sag lifts its centre, a crest lowers it,
    stiffness-split, band-clamped, anchors fixed.  Ring CORNERS are real
    grade breaks — a triple only fairs when the boundary is straight
    through it (bend ≤ ``max_bend_deg``).  Runways/buildings excluded
    (own profile / flat).  Mutates ``elev``; returns residual kinks.

    ``law_adjacency`` (``{node: [(other, budget_m), …]}``) makes the pass
    LAW-GUARDED: every move is clamped into the interval the node's law
    edges allow around the CURRENT neighbour values, and a node already
    outside that interval (a projection-declared residual) or with an
    infeasible interval never moves.  Required when the caller runs this
    AFTER its last feasibility projection — the unguarded pass left
    junction MESH chords (pairs crossing between two ring runs, which no
    triple sees) a median 1.8 cm over budget (SPJC 2026-07-05, the
    43-pair cm-noise class).  ``None`` ⇒ unguarded (solve-time call: the
    final projection re-enforces every pair the fairing perturbs)."""
    import math as _math
    from auto_patch.layout import (ROLE_RUNWAY, ROLE_BUILDING,
                                   ROLE_BOUNDARY, ROLE_GROUNDSIDE_PAVEMENT)
    _SKIP_ROLES = {ROLE_RUNWAY, "runway_crossing", ROLE_BUILDING,
                   ROLE_BOUNDARY, ROLE_GROUNDSIDE_PAVEMENT,
                   "retaining_wall", "tunnel_ramp", "clearance",
                   "taxiway_clearance",
                   # Service roads are DEM-follow ramps with genuine
                   # grade breaks at mouths/portals whose weld pins are
                   # not in every caller's hard set — fairing them
                   # minted 0.2-0.9 m bumps against the welds (CYXY
                   # #201: 132 %).  The waviness law is an AIRCRAFT
                   # taxiway ride-quality rule; roads keep their ramps.
                   "service_road", "service_junction"}
    cps = layout.canonical_points
    n = len(elev)
    n_band = len(node_band) if node_band is not None else 0

    # Building pads are FLAT by ruling — fairing must never move a node
    # a pad ring OWNS, even when that node is encountered on a
    # NEIGHBOUR's ring (CYXY building9: fairing the adjacent junction
    # ring dragged the shared corner 0.16 m off the pad level; the pad
    # flat group ran BEFORE this pass and could not restore it).  Pad
    # ring nodes therefore join the anchor set for every caller.
    building_ring_nodes: set = set()
    for s in layout.shapes:
        if (s.role != ROLE_BUILDING or s.polygon is None
                or s.polygon.is_empty):
            continue
        try:
            for (x, y) in s.polygon.exterior.coords:
                i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
                if i is not None and i < n:
                    building_ring_nodes.add(i)
        except Exception:
            continue
    if building_ring_nodes:
        anchors = set(anchors or ()) | building_ring_nodes

    # ── Precompute fairable TRIPLES once (geometry never changes) —
    # the sweeps then run on plain tuples, no per-sweep geometry work.
    triples = []          # (a, b, d, l1, l2) — flat list, for the sweeps
    for s in layout.shapes:
        if s.role in _SKIP_ROLES or s.polygon is None \
                or s.polygon.is_empty or s.polygon.geom_type != "Polygon":
            continue
        coords = list(s.polygon.exterior.coords)
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        m = len(coords)
        if m < 4:
            continue
        idx = [bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
               for (x, y) in coords]
        for t in range(m):
            a, b, d = idx[(t - 1) % m], idx[t], idx[(t + 1) % m]
            if b is None or a is None or d is None:
                continue
            if b >= n or a >= n or d >= n or b in anchors:
                continue
            (xa, ya), (xb, yb) = coords[(t - 1) % m], coords[t]
            (xd, yd) = coords[(t + 1) % m]
            l1 = _math.hypot(xb - xa, yb - ya)
            l2 = _math.hypot(xd - xb, yd - yb)
            if l1 < min_seg_m or l2 < min_seg_m:
                continue
            dot = ((xb - xa) * (xd - xb)
                   + (yb - ya) * (yd - yb)) / (l1 * l2)
            if dot < _math.cos(_math.radians(max_bend_deg)):
                continue                      # corner — real grade break
            triples.append((a, b, d, l1, l2))

    if not triples:
        return 0

    # A direct straight-line fit per run was MEASURED and rejected
    # (2026-07-04, user suggestion): assigning the chord between run
    # endpoints (band-projected, small-move-guarded) reads smoother in
    # theory, but band clamps and cross-run neighbour pairs make the
    # chord not-quite-feasible in practice — CYXY within-shape rose
    # 182 → 237-256 for no visible gain over POCS-from-seed (the solved
    # seed is already near-linear; the POCS converges in a few cheap
    # sweeps on the precomputed triples).
    for _sweep in range(max_sweeps):
        worst = 0.0
        for (a, b, d, l1, l2) in triples:
            g1 = (elev[b] - elev[a]) / l1
            g2 = (elev[d] - elev[b]) / l2
            dg = g2 - g1
            lim = k_rate * 0.5 * (l1 + l2)
            ex = abs(dg) - lim
            if ex <= 1e-6:
                continue
            delta = _math.copysign(ex, dg) / (1.0 / l1 + 1.0 / l2)
            nb = elev[b] + delta
            band = node_band[b] if b < n_band else None
            if band is not None:
                lo, hi = band
                if lo <= hi:
                    nb = min(max(nb, lo), hi)
            if law_adjacency is not None:
                incident = law_adjacency.get(b)
                if incident:
                    law_low = law_high = None
                    for (other, budget) in incident:
                        if other >= n:
                            continue
                        other_value = elev[other]
                        low = other_value - budget
                        high = other_value + budget
                        if law_low is None or low > law_low:
                            law_low = low
                        if law_high is None or high < law_high:
                            law_high = high
                    if law_low is not None:
                        if (law_low > law_high
                                or not (law_low <= elev[b] <= law_high)):
                            continue    # infeasible / already-outside: never move
                        nb = min(max(nb, law_low), law_high)
            moved = abs(nb - elev[b])
            if moved:
                elev[b] = nb
                if moved > worst:
                    worst = moved
        if worst < tol:
            break
    n_over = 0
    for (a, b, d, l1, l2) in triples:
        g1 = (elev[b] - elev[a]) / l1
        g2 = (elev[d] - elev[b]) / l2
        if abs(g2 - g1) - k_rate * 0.5 * (l1 + l2) > 1e-4:
            n_over += 1
    return n_over


def _solve_spine_profile(elev, base_hard, spine_adj, spine_floor,
                         node_band=None, nodes_xy=None,
                         *, max_sweeps=5000, tol=1e-3, curvature=0.25):
    """Dedicated SMOOTH spine solve on the unified graph's geometry nodes.

    Min-curvature (inverse-budget² harmonic mean blended with the plain mean),
    clamped into the neighbour cap slabs ``[z_j − budget, z_j + budget]``, the
    building-frontage floor, AND the per-node REACH BAND ``node_band[i] =
    (floor, ceiling)`` (user 2026-06-26) — so the spine is closest-DEM-FEASIBLE
    too: it can't sit BELOW its reachable floor (CYXY TX3 at 677 when its floor is
    ~685) nor above its ceiling.  Anchors = the nodes already HARD (runway
    contacts at their LOCAL runway elevation + tile seams).  Mutates ``elev`` in
    place; returns the set of spine node indices it solved (to be frozen for the
    body fill)."""
    import math
    INF = float("inf")
    anchors = {i for i in spine_adj if i < len(base_hard) and base_hard[i]}
    nodes = [k for k in spine_adj if k < len(elev)]
    free = [k for k in nodes if k not in anchors]

    def _band(k):
        b = node_band[k] if (node_band is not None and k < len(node_band)) else None
        if b is None:
            return -INF, INF
        lo, hi = b
        return (lo, hi) if lo <= hi else (0.5 * (lo + hi), 0.5 * (lo + hi))

    # warm start free nodes onto their reach-band floor / serving floor (fill UP
    # out of a wrong-low DEM; the serving arm climbs to its pads).
    for k in free:
        bf, _bh = _band(k)
        f = spine_floor.get(k, -INF)
        target = max(bf, f)
        if target > -INF and target > elev[k]:
            elev[k] = target
    for _ in range(max_sweeps):
        moved = 0.0
        for k in free:
            nb = spine_adj.get(k, ())
            if not nb:
                continue
            sw = acc = 0.0
            for (j, w) in nb:
                wt = 1.0 / max(w, 1e-3) ** 2
                sw += wt
                acc += elev[j] * wt
            harm = acc / sw if sw > 0 else elev[k]
            pm = sum(elev[j] for (j, _w) in nb) / len(nb)
            tgt = (1.0 - curvature) * harm + curvature * pm
            lo, hi = _band(k)
            for (j, w) in nb:
                if elev[j] - w > lo:
                    lo = elev[j] - w
                if elev[j] + w < hi:
                    hi = elev[j] + w
            f = spine_floor.get(k)
            if f is not None and f > lo:
                lo = f
            tgt = (min(max(tgt, lo), hi) if lo <= hi else 0.5 * (lo + hi))
            d = tgt - elev[k]
            if d:
                elev[k] = tgt
                if abs(d) > moved:
                    moved = abs(d)
        if moved < tol:
            break
    # FAIRING (task 3): bound the grade CHANGE along every spine chain by
    # the taxiway vertical-curve rate — runs after the harmonic solve
    # (which minimises grade, not grade CHANGE, so it still tracks DEM
    # noise in legal ±cap wiggles) and before the exact cap projection.
    if nodes_xy is not None and _os.environ.get("O4_SPINE_FAIRING",
                                                "1") == "1":
        from auto_patch.config import TAXIWAY_MAX_GRADE_CHANGE_PER_M
        n_kink = _fair_spine_chains(elev, spine_adj, anchors, node_band,
                                    nodes_xy,
                                    TAXIWAY_MAX_GRADE_CHANGE_PER_M)
        if n_kink and _os.environ.get("O4_STEP_DEBUG") == "1":
            print(f"    [fairing] {n_kink} spine triple(s) over the "
                  f"vertical-curve rate after fairing (anchor/band-forced)")

    # Final EXACT cap-Lipschitz projection on the spine edges (only the runway/
    # seam anchors are hard) — the Gauss-Seidel's harmonic compromise can leave a
    # ~cap residual where several centerlines meet at a junction node; this drives
    # every free↔free spine pair ≤cap (a both-anchor pair stays = genuine step).
    from .one_solve import feasibility_project
    s_edges = []
    seen = set()
    for i, lst in spine_adj.items():
        for (j, w) in lst:
            e = (i, j) if i < j else (j, i)
            if e in seen:
                continue
            seen.add(e)
            s_edges.append((e[0], e[1], w))
    feasibility_project(elev, [{"edges": s_edges}], anchors)
    return set(nodes)


def _merge_spine_adj(a, b):
    """Union two ``{i: [(j, budget), ...]}`` spine adjacencies (the apron/junction
    consecutive chain + the unified graph's rect-axis links), keeping ONE edge per
    pair (min budget = tightest cap)."""
    out: dict = {}
    seen: dict = {}
    for src in (a, b):
        for i, lst in src.items():
            for (j, w) in lst:
                e = (min(i, j), max(i, j))
                if e in seen:
                    if w < seen[e]:
                        seen[e] = w
                    continue
                seen[e] = w
    for (i, j), w in seen.items():
        out.setdefault(i, []).append((j, w))
        out.setdefault(j, []).append((i, w))
    return out
