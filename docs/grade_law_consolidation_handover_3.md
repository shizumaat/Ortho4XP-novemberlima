# grade_law consolidation — handover #3 (continues handover #2)

> ⚠ **SUPERSEDED (2026-06-30 audit) by `grade_law_consolidation_handover_4.md`.**
> Its main open item (spine rising to serve a building across an apron) was re-diagnosed
> and resolved there (taxi centerlines by connectivity, commit `adc6bac`).

Read `docs/grade_law_consolidation_handover_2.md` first. This records the
2026-06-29 session. Goal unchanged: ONE canonical ruleset (`grade_law`) both the
solver and validators use; no airport legitimately infeasible.

## DONE this session (working tree, NOT yet committed — verify suite first)

### 1. Runway-anchor coverage fix (`grade_graph._runway_anchors`)
A taxiway that MEETS the runway already has its contact materialised as ONE welded
canonical node (runway sub-rect corner + abutting junction vertex + taxi SPINE
node, all sharing the point — the segmenter cut it, `_unify_airside_geometry`
welded it). It was simply never made a `G.runway_anchor`, because the old logic
anchored only the nearest emitted node within `_NEAR_M=18 m` of a centerline
ENDPOINT — and CYXY taxiway A's centerline endpoint is the runway-edge MIDLINE at
(-345,914), whose welded side nodes (387/392) are 22.6 m away (>18 m).
**FIX**: a 2nd pass anchors every spine node coincident with a runway vertex (i in
`G.spine_adj` AND a runway ring vertex) at that runway's
`_sample_runway_segment_elev`, GATED to nodes within `_EDGE_REACH_M=30 m` of a
TERMINATING contact endpoint (a centerline endpoint within `_CONTACT_M` of the
runway). The terminate-gate is ESSENTIAL: anchoring every runway-edge spine node
unconditionally over-constrains a taxiway that merely CROSSES / runs ALONG a runway
mid-centerline and broke SPJC's hard `route_band=0` gate (SPJC taxiway F, 36 m from
its endpoint → 0.16 m floor flag). Gate `O4_RUNWAY_CONTACT_ANCHOR` default ON.
RESULT: CYXY 6→12 anchors; nodes 387/392 anchored @694; **building #21 (centroid
(-638,668), 754 m²) ceiling 705.77→699.50, EMITTED SEAT 702.2→699.1** — the
over-credit the whole solve hinged on (memory `runway_anchor_coverage_critical`).
Full suite verified 19-baseline at this fix alone.
★ The USER's first instinct (cut the runway at the taxi crossing) was BUILT and
measured a NO-OP and reverted: the runway is already cut there; the welded node
already exists; the gap was purely the missing ANCHOR. Don't re-try the cut.

### 2. Constant consolidation into `grade_law` (one source, no local copies)
User directive: every grade/cap/length rule lives once in `grade_law`, no "5
variations and local copies." Done (all value-identical → behaviour-neutral):
- `grade_law` now imports+exposes `APRON_MAX_GRADE`, `TAXI_MAX_GRADE`, and defines
  `RUNWAY_CONTACT_M=12`, `RUNWAY_JOIN_NEAR_M=18`, `BUILDING_REACH_CORRIDOR_M`.
- Removed: `building_feasibility._APRON_CAP` (=APRON_MAX_GRADE), `_ENTRY_CAP`
  (=TAXI_MAX_GRADE, was dead), `_CONTACT_EDGE_TOL_M` (dead). Now use grade_law.
- `grade_graph._runway_anchors` + `grade_graph_validate` runway-join: `_CONTACT_M`/
  `_NEAR_M` literals → `grade_law.RUNWAY_CONTACT_M`/`RUNWAY_JOIN_NEAR_M` (was 3
  duplicate copies incl. `lateral_spine_nodes._RUNWAY_TOL_M`).
- Corridor: `BUILDING_FRONTAGE_CORRIDOR_M`(200) + `BUILDING_SPINE_LIFT_CORRIDOR_M`
  (350, gated `O4_LONG_SPINE_LIFT`) → ONE `BUILDING_REACH_CORRIDOR_M=200` (the
  established default; the 350 gate was a vestigial experiment, REMOVED). 200 keeps
  SPJC `route_band=0`; 350 surfaced 2× sub-0.25 m ceil at SPJC.

## ★★ RESOLVED in handover #4 — read that first

> The OPEN item below is **resolved** — but the diagnosis here (a "REGION lift"
> problem; the 5 reverted approaches) was WRONG. The real root was a
> data-categorization bug: `taxi_centerlines` grouped edges BY NAME and severed the
> continuous `~U12→F` route into a dangling orphan apron that broke the spine. Fixed
> by building centerlines BY CONNECTIVITY (`docs/grade_law_consolidation_handover_4.md`,
> memory `taxi_centerline_connectivity_model`). **Do not re-try the approaches below.**

## ★★ OPEN (SUPERSEDED — see handover #4) — the spine does NOT rise to serve a building ACROSS an apron

This is the real remaining problem (CYXY north apron: building18 #17 @700.4 /
building22 #21 @699.1 / building15 #15, all >100 m from their ~U12 spine across one
continuous apron; the apron transition to the sagged spine is 2.4–3.5 %, the 15.5 %
cliff family). The building SEATS are now correct (#21 = 699.1); the SPINE under
them sags to the runway. NOT yet fixed — every approach tried this session traded
the dip for a worse failure. Documented so the next attempt starts informed.

### How the seat is set (verified, for reference)
`reach_band_unified.band(x,y)` → `(floor, ceiling)`. ceiling = `min over runway
anchors (anchor_elev + capdist[k*] + ecap·arc + perp_climb)` where k* = nearest
spine node to the foot's projection on the nearest-visible serving centerline,
`capdist` = cap-Dijkstra (Σ cap·dist) from the runway anchor over `G.spine_adj`,
`perp_climb = ecap·min(perp,7.5) + APRON_MAX_GRADE·max(0,perp−7.5)`. Building level
= `clamp(DEM, floor, ceiling)`. So the seat is computed AS IF the serving spine node
sits at its own ceiling and the apron climbs ≤cap out to the pad.

### Why the spine sags (root)
`_solve_spine_profile` is a min-curvature solve whose ONLY hard anchors are the
runway (and seams, and building pads that literally sit ON a centerline). A
building ACROSS an apron is connected to the spine by NOTHING — `G.spine_adj` has
only centerline↔centerline edges; the building's reach lives only in the band. So
the spine has no upward anchor and sags to the runway. The band's ceiling is used
only as a passive upper CLAMP, never a target. → building at top of band (ceiling),
spine at bottom (runway). Same graph, opposite ends.
The south complex works ONLY because CYXY building #5's pad sits on taxiway G (a
genuine on-spine hard anchor); G/A2 then grade down from it correctly.

### USER's intended architecture (the target design)
EVERY building AND EVERY no-building apron should be a HARD anchor in the one spine
graph — like the runway — connected to its serving spine node(s) by the SAME node +
reach the band used (band should RETURN its serving node so seat & spine are
identical by construction). The spine solver then finds the min-cap profile between
all anchors.

### Approaches tried this session — ALL reverted, each fails a different way
1. **Soft floor enforced** (`_spine_floor_per_node` clamp in GS): sags — the final
   cap projection (runway-only hard) discards it.
2. **Hard-pin floored nodes** (add to spine anchors): 22 fails — pinning the spine
   breaks the spine GRADE (the floors aren't cap-Lipschitz where the ceiling is
   non-smooth → 881 % step); also broke `cyxy_spine_zero`, `route_band[SPJC]`.
3. **POCS lower-bound** (lift to floor, cap-project, iterate): order-dependent —
   end-on-project sags, end-on-lift gives 19 spine violations (881 %).
4. **Synthetic edge to NEAREST-polygon spine node** (`apron_cap·chord`): north
   improved (b22 2.15→1.19 %) but 12 SOUTH spine violations (9.3 %) — wrong node
   (not the band's k*) + the per-node floor over-reached onto ~U12's far west end
   next to the low west apron.
5. **Synthetic edge to the BAND's exact serving node** (`band(...,serving=True)`):
   22 spine violations (5.1 % @ node 658). The point-anchor drives k* to its ceiling
   but k*'s OTHER spine neighbours (branches toward the runway / west apron) aren't
   anchored and stay sagged → the step just moves to those edges.

### The crux for the next attempt
It is a **REGION** problem, not a point anchor. A building must lift the whole
apron/spine REGION it serves, CONSISTENTLY and BOUNDED to that region — lifting one
node steps to its un-lifted neighbours (approach 5); lifting every node in a radius
over-reaches into another feature's region (approach 4). Likely needs: segment the
apron into the region each building/apron serves, and lift that region's spine sub-
chain together to the building's reach (cap-Lipschitz within the region), with the
runway anchor bounding the low end. The band ALREADY knows each point's serving
node + reach — expose it and use it to define the regions. NOTE the ceiling uses the
cap-Dijkstra SHORTEST-budget path, which may include flat rect-end links (budget
≈0) and may NOT be the local serving chain — verify the building's ceiling is
reachable along the LOCAL chain before anchoring to it, or the anchor is infeasible.

## Verification recipe (unchanged)
venv; `PYTHONHASHSEED=0 PYTHONPATH=src:.:tests`; `venv/bin/python -m pytest tests/
-q`. Baseline = 19 failures (handover #2). Quick spine/apron probe pattern:
build CYXY, map vertex→emitted alt, compare each north building's seat vs its
nearest-centerline foot emitted (apron %), and `within_violations(L)` filtered to
`v[4]` (spine within-shape). `cached_airport_layout` CACHES — use
`build_airport_pavement` for fresh builds when iterating on the solver.
