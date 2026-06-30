# Route-profile solver — status (next-gen one-profile solve)

> ⚠ **SUPERSEDED (2026-06-30 audit).** Mid-evolution snapshot. The `O4_ROUTE_PROFILE_SOLVE`
> gate and sibling modules (`route_graph.py`, `envelope.py`, …) it describes are gone;
> `route_profile` is now the only solver. Live model → **`anisotropic_edge_handling_plan.md`**.
> Acknowledged-open items (suite reds, SPJC spine 0→2 regression) → **`OPEN_ITEMS.md`**.

Implements `docs/one_profile_solve.md` as a clean-room package.  **LIVE IN DEV by
default** (`O4_ROUTE_PROFILE_SOLVE=1`, user 2026-06-25 — for X-Plane review;
`=0` falls back to legacy).  `unified_jacobi.py` is untouched.

---
# ★ HANDOVER (2026-06-25 PM) — THE SINGLE-GRAPH ROUTE SOLVE IS DONE; EMISSION IS NEXT

**What is LIVE right now:** the live solver is still `route_profile/solve.py` →
`one_profile_solve` (`one_solve.py`), giving **CYXY 469 within-viol / spine 0**,
with the spine climbing to serve its buildings (this session's banked win — the
trace-anchored, chain-Lipschitz building floor).  This is unchanged and shippable.

**What is NEW and PROVEN but NOT yet wired into the live path:**
`route_profile/route_graph.py` — `solve_route_graph()` — the consolidation the
user drove this session.  It solves the route profile **on the single graph**
(`taxi_routing.shared_taxi_route_graph`, the SAME graph the band/building
elevations use) and **CYXY solves with ZERO over-cap residual** while the band
stays **byte-identical** (building19 700.47, building16 708.69, Δ0.0000).
Verified by `/tmp/g2.py`.

Key properties of `route_graph.solve_route_graph` (read the module docstrings):
- `enrich_route_graph()` weaves every geometry route node + a synthetic node at
  each rect end into G by **splitting existing edges** (unique `(int,int)` keys
  far outside the coord range so nothing merges; nearest-edge assignment;
  snap-to-endpoint; merge splits <2 m).  Distance-preserving → band unchanged.
  ⚠ `new_cap = dict(G.edge_cap)` RETAINS the original direct-edge caps — the
  band's `band(x,y)` looks up `edge_cap[(kA,kB)]` for two CONSECUTIVE centerline
  vertices, and splitting that edge would default it to 1.5 % and collapse a 3 %
  taxiway's budget (building16 dropped 6.4 m before this).  The Dijkstra only
  walks the adjacency, so the stale direct entry is harmless to it.
- Contacts come from `building_feasibility._runway_route_contacts` — extracted
  this session as the ONE shared source; the band was refactored onto it
  (byte-identical).
- The solve is **pure min-curvature between the real anchors** (runway contacts +
  building floors); **NO DEM target, NO reach-band clamp on the route z** (those
  are not anchors — removing them cleared the F×14R/32L and E×02/20 "conflicts").
- Building↔spine coupling is **two-sided**: a building floors the spine
  (`level − perp_climb`, clamped to each node's band CEILING) AND ceils it
  (`level + perp_climb`, clamped to the band FLOOR), both measured by the SAME
  perp-climb rule the band uses (corridor first 7.5 m at the route's own cap,
  rest at 1 %).  Getting these rules to match the band is what took the residual
  to 0.  (The ceiling is a no-op on CYXY — all buildings sit above the bowl — but
  is needed where terrain drops below the runway.)
- Returns `(z, residual, geo_key, rect_end_keys, bfloor)`.  `geo_key`
  `{spine_node_idx → graph_key}` and `rect_end_keys` `{id(rect) → (k0,k1)}` are
  the **read-by-index** maps for emission.

## THE REMAINING TASK: wire the read-by-index EMISSION (task #6)
Apply the solved profile to geometry as a PURE READ in `solve.py`:
- spine / junction-spine nodes → `elev[i] = z[geo_key[i]]`;
- each sloping rect → `altitude_high/low` from `z[rect_end_keys[id(rect)]]`
  (and its cap reads its end nodes → co-planar by construction);
- buildings → their seat; runway / seam → kept;
- then `_writeback`, then validate emitted within-grade + spine on CYXY and the
  other three fixtures (HECA, SPJC, SPLP).

### ★ OPEN DESIGN QUESTION (user, must investigate FIRST):
**Do we solve the remaining (body) node elevations IN THE GRAPH and feed them to
the geometry, or do we still grade the "body" shapes (apron / junction interiors)
AFTER the skeleton is applied?**  The route skeleton (spine + rect ends) is on
the graph and reads back by index.  The apron/junction BODY interiors are NOT on
the route graph.  Two candidate models:
1. **All-in-graph:** put the body nodes into the one graph too (every geometry
   node), solve their elevations there (grading from the skeleton, ≤cap, closest
   reachable), then emission is a pure read for EVERYTHING.  (Aligns with "one
   graph, geometry just reads it"; no post-apply grading.)
2. **Skeleton read + body fill:** read the skeleton from the graph, then grade
   the body shapes from the now-fixed skeleton (a light 2-D fill, or `body_z` =
   closest-DEM within `route ± apron-cap·perp`).
Decide this BEFORE wiring — it determines whether emission is purely a read or a
read-plus-fill.  Note: every attempt this session to grade the body in the OLD
2-D `one_solve` while pinning the route fought itself (cap squeeze, junction
steps); the all-in-graph route (option 1) is the direction that has not yet been
tried and is most consistent with the single-graph model.

## Probes (in /tmp, rebuild as needed; PYTHONHASHSEED=0; builds 60–90 s)
- `/tmp/g2.py` — band-unchanged + route-graph residual (the main check).
- `/tmp/route_match.py`, `/tmp/floor_vs_band.py` — the floor-vs-band rule checks.
- `/tmp/conflict_detail.py` — JOSM lat/lon for any residual edge.

## Dead / shelved (gated off, safe to DELETE during cleanup):
`route_profile/caps.py` (per-shape cap stamp — dead end), `graph_field.py`
(resampled-graph pure-read — superseded by route_graph), `profile.py` (early
profile probe).  In `solve.py`: `_add_rect_end_nodes` + `_route_node_set` +
the `O4_RP_GRAPH_FIELD` / `O4_RP_RECT_BRIDGE` gated branches (both default off).
None are wired into the live path.  ⚠ RULE FROM USER: do NOT build another graph
without explicit permission — solve on `shared_taxi_route_graph`.

---

## Live model (2026-06-25) — ONE GRAPH
There is a single reachability graph: the taxi-route reach band
(`building_feasibility.reach_band_sampler`).  It sets the building levels AND
bounds the spine; the apron grades FROM the spine.  No second reachability graph
(the earlier `cap_budget_envelope` was removed — it disagreed with the buildings
because it routed over the shape graph, not the taxi route).

- **Spine** (taxi centerline nodes): bounded by the band, clamps only to its
  centerline-consecutive chain → spine ≤cap.
- **Buildings**: flat at the MEDIAN band ceiling over their frontage ring
  (centres the apron twist).
- **Apron body**: grades from the spine/buildings via the projection (neighbour
  cap slabs); no direct band bound.
- **Apron↔taxi blend** (`config.APRON_TAXI_BLEND`, shared grade_graph): an apron
  edge's ALONG-route component earns the route's cap as it nears a taxiway
  (decays past `APRON_TAXI_TRANSITION_M=30`); at a BUILDING FRONTAGE the blend is
  isotropic and targets the 4% back-edge cap (the warp/twist that blends a flat
  pad into the climbing route).  Solver + validator share this cap.

## Current numbers (PYTHONHASHSEED=0, grade_graph_validate)
| airport | total within-viol | spine |
|---|---|---|
| CYXY | 470 | 0 |
| HECA | 4538 | 0 |
| SPJC | 447 | **2** ⚠ |
| SPLP | 114 | 0 |

A2 climbs 696→707; no bowl; spine RISES to serve buildings (building19
694.5→699.0).  ⚠ SPJC spine 2 — pre-existing baseline regression, to chase.
(Earlier numbers in git history reflect the pre-spine-climb model.)

## RESOLVED (2026-06-25): trace-anchored spine climb (NOT band-Lipschitz)
The building-out-runs-spine inconsistency is fixed by making the **spine RISE to
serve its buildings**, NOT by lowering buildings.  Decisive probe (CYXY
building19): the building's feasible level (700.2) is correct and its serving
through-taxiway ~U12 *can* climb to 699.9 (its band ceiling) — the band/building
source was never wrong; the SOLVE just smoothed ~U12 flat at 694.5 and never
lifted it.

- **Band-Lipschitz tightening (user option 1) was tried and REJECTED**: it makes
  the band grade-consistent by taking `ceil_i = min_j(ceil_j + cap·dist)`, which
  pulls the *building* DOWN to the flat spine (building19 700.2→698.6) — the wrong
  direction (deepens the bowl, second-guesses the authoritative source).  Code
  was written then removed.
- **The fix (`anchors.building_spine_floor`, live):** the SAME trace that sets a
  building's feasible level (nearest VISIBLE centerline + perpendicular foot)
  anchors the spine node at the foot at `seat − APRON·perp` (the 1 % apron climb
  spine→pad), then that anchor is propagated along the consecutive centerline
  chain as a **cap-Lipschitz floor** (`floor_j = anchor − capdist(foot→j)` over
  `spine_adj`, clamped to each node's band ceiling).  The whole ramp climbs
  smoothly; the floor is grade-consistent by construction (can't create a spine
  break) and — because every chain node's neighbour is also floored — it is no
  longer dropped by the one_solve "envelope yields" fallback (which silently
  killed the old single-node floor → arm stayed flat).
- **Why a hard single-node anchor was insufficient:** it lifts one node but its
  consecutive neighbour stays pinned low → a 12 m spine break (SPLP spine 0→2).
  The chain-propagated floor lifts the neighbours too.

Numbers (within-viol / spine, PYTHONHASHSEED=0): CYXY 860→**470**/0 · HECA
4507→4538/0 · SPJC 679→**447**/2(pre-existing) · SPLP 128→**114**/0.  building19
spine 694.5→699.0; buildings unchanged at feasible levels (bowl median 3.0 m).

### STILL OPEN — the climb "landing" (transition zones)
Worst remaining CYXY spots are 15 % over 12 m on junction/apron BODY where the
climbed spine meets DEM-following body that can't keep up.  HECA body count is
neutral vs baseline for the same reason (huge terminal, long ramps).  This is the
transition-zone problem (apron/junction body grading FROM the climbed spine), the
next layer — NOT a spine problem (spine=0 holds).

---
## (historical) original status

## Where it lives
`src/auto_patch/elevation_per_surface/route_profile/` (614 lines total, all
modules < 260 lines):
- `solve.py` — orchestration; reuses the elevation-neutral primitives from
  `unified_jacobi` (`_build_node_list`, `_seed_elevations`, `_sample_node_dem`,
  `_build_shape_constraints`, `_build_level_coupling`, `_writeback`).
- `anchors.py` — building seats (`building_feasibility.building_feasible_levels`)
  + the apron-body role set.
- `envelope.py` — the cap-budget reachability envelope (multi-source Dijkstra).
- `spine.py` — the taxi-centerline sub-graph (consecutive on-line nodes).
- `one_solve.py` — the projected Gauss-Seidel one solve.

Dispatch seam: `solver.solve` branches on `ROUTE_PROFILE_SOLVE` → leaves
`unified_jacobi.solve` byte-identical when off.

## The model (one rule)
`z_i = clamp(DEM_i, floor_i, ceil_i)` where `[floor, ceil]` is the cap-budget
reachability envelope of ALL anchors (runway contacts + tile seams + building
pads), computed by two multi-source Dijkstras over the within-shape grade graph
(edge weight = the per-edge budget `cap·length`):

    ceil_i = min over anchors a of (elev_a + capdist(a → i))
    floor_i = max over anchors a of (elev_a − capdist(a → i))

This is the user's constructive trace ("grade back from every anchor at cap; the
binding route wins") generalised to 2-D — running on the shape graph (not the
1-D centerline) is what couples a building to an adjacent rect THROUGH the apron,
so **A2 climbs** (building16's budget reaches A2's end via the apron).

Per-role target inside the envelope (bounds shared):
- apron body → closest-to-DEM; route (rect ends) → smoothest (min curvature).
- Over-constrained nodes (`floor > ceil`, a high building vs low runway over a
  short chord) collapse `floor → ceil` (sit at max reachable; ≤cap by
  construction).
- **Spine nodes clamp ONLY to their centerline-CONSECUTIVE neighbours** (a 1-D
  always-feasible chain) so the apron yields to the spine.  The spine chains come
  from the SAME `grade_graph` the solver grades on and the validator checks
  (`spine.py` calls `grade_graph.shape_constraints` via the solver's context) —
  one graph, no membership drift.  Where the DEM-reach envelope conflicts with
  the centerline within-grade, **the envelope yields to the spine** (the frontage
  takes the step, not the spine).
- The final rect flat-end **coupling conforms to the spine** (a spine node in a
  rect-corner group is authoritative; averaging it away was silently breaking the
  spine — that one fix took CYXY 76→0, HECA 106→0).

## Current results (PYTHONHASHSEED=0)
| airport | total within-viol (new → legacy) | spine (new → legacy) |
|---|---|---|
| CYXY | **897 → 1254** | **0** → 0 |
| HECA | **3336 → 17286** | **0** → 0 |
| SPJC | **220 → 1034** | **0** → 0 |
| SPLP | **147 → 183** | **0** → 0 |

- **Spine = 0 on ALL four fixtures.**
- **A2 climbs** 696→707 (legacy emits it FLAT 696.4 — the original bug). 0 flat
  sloping-rects on CYXY.
- **No bowl**: CYXY building16=708.7 (≥706), building19=700.5 (≥698).
- `test_cyxy_spine_zero_no_bowl` PASSES under `O4_ROUTE_PROFILE_SOLVE=1`.
- Surfaces far smoother overall (HECA total −81%).

## Post-solve elevation invariant (ESTABLISHED)
Traced every pass that can modify airside elevations AFTER the final solve
(pipeline.py:4023).  On single-tile airports: NONE — `_smooth_junction_ring_
curvature` and all feature passes leave airside untouched.  On multi-tile
airports the ONLY modifier was the post-solve `cut_layout_at_tile_boundaries`
seam re-sample: airside is cut PRE-solve (pipeline.py:3716) and graded against
the seam DEM anchors, but the gapped edge grazes the post-solve cut buffer and
got a spurious NN re-sample (SPLP 74.30→72.60 = the 2 violations).

FIX (landed, gated): `cut_layout_at_tile_boundaries` gains a ``skip_roles`` param;
the two POST-solve calls pass the airside roles when `O4_ROUTE_PROFILE_SOLVE` is
on, so airside is frozen (the solve owns it).  Legacy path unchanged
(``skip_roles=frozenset()`` → byte-identical; verified SPLP legacy spine 0,
total 183 unchanged).  Confirmed: under the new solver the ONLY airside
elevation change is the legitimate PRE-solve cut — **zero post-solve airside
modifications**.  Files touched outside the package: `tile_cut.py` (+skip_roles),
`pipeline.py` (+gated skip at the 2 post-solve calls), `solver.py` (dispatch).

## Remaining work (retirement)
- Run the full grade/geometry suite under the gate; triage every regression
  (the big unknown — only spine/bowl on 4 airports checked so far).
- Re-cut compare-target fixtures to the new solver's output.
- Extend `grade_graph_validate` to cover rects (plan item).
- Flip the gate default → on; then DELETE the ~15 dead legacy passes and
  relocate the KEEP primitives out of `unified_jacobi`.

---
# ★ ROUTE-GRAPH REDESIGN PLAN (2026-06-26, user-driven)

**Principle (user):** build the route graph RIGHT the first time and read elevations
DIRECTLY from it by index — never patch emitted elevations, never re-stamp, never
soft-seed-then-move.  If a surface is wrong, fix the GRAPH.

**One airside route graph** provides elevations for everything:
- **Taxi routes** (aircraft): spine, per-letter cap (1.5%/3%), anchored at the RUNWAY.
- **Service roads** (trucks): spine too, 4% cap, anchored at DEM where they enter the
  perimeter/landside road network AND at airside contacts, graded ≤4% between.
- **Rects** (taxiway segments): a flat-end NODE at each short-edge midpoint, connected
  along the rect AXIS (extend from the nearest centerline endpoint when the centerline
  ends short — NOT a ≤3 m snap).  Every rect is a graph segment.
- **Caps**: a BRIDGE EDGE rect-end-node ↔ junction-node (rect elevation one side,
  junction the other), solved in the graph, read by index.  NO re-stamp.
- **Discovered (TX) taxiways**: fold ``layout._discovered_centerlines`` into the graph
  source (currently excluded → TX rects 90–540 m off-graph).

**Seating (reachability consumers):**
- Buildings → reachable level via nearest visible taxi route (have).
- Aprons WITH a building → from the building frontage (have).
- Aprons WITHOUT a building → reachable level via WHICHEVER route reaches (taxi OR
  service road); graded WITHIN the band (min-curvature surface, not flat); if neither
  reaches → visible-chord patch to nearest reachable pavement.

**Emission** = pure read-by-index of the solved graph (spine + rects + caps + service
spines, HARD); body grades to it.  Body compliance is the NEXT layer (user: spine
perfect first).

**Validator** (``grade_graph_validate.within_violations``) now covers rects + caps +
runway-joins, width-based, flagged ``is_spine`` — extend to service spines too.

WHY synthetics were missing (diagnosed): enrich only snapped rect-end midpoints to
existing edges within 3 m; TX centerlines not in the source; named taxiway centerlines
end 7–20 m short of the rect's physical end.  Route-graph SOLVE is clean (residual 0) —
the gap is graph COVERAGE, and the emission drifting soft rects off the graph z.

---
# ★ ONE-GRAPH UNIFICATION PLAN (2026-06-26) — the authoritative next-session plan

ROOT (proven this session): elevations are SET on the route graph (Graph A,
apt.dat centerline nodes + synthetics) and CHECKED on the grade graph (Graph B,
geometry ring vertices), bridged by `geo_key`, with TWO context builders
(`unified_jacobi._grade_graph_context` vs `grade_graph_validate._context`).  So
Graph A can be internally ≤cap while Graph B (the validator) reports violations.
The 18 residual CYXY spine errors are entirely this drift (spine↔runway at
runway-adjacent junctions, junction-spine pairs Graph A never had as edges).

The fix is to make it genuinely ONE graph:

1. **One node set.**  The solver must set elevations on the SAME nodes the
   validator checks — the geometry ring vertices (`bucket_to_idx`), not a
   parallel route-graph node set read back via `geo_key`.  Either (a) build the
   route graph ON the geometry vertices (every spine/rect/cap/junction vertex is
   a graph node), or (b) have the route solve write directly into `elev[idx]` for
   every validator node and never let a second pass move it.

2. **One context builder.**  Delete the duplication: `spine.spine_adjacency` +
   `unified_jacobi._build_shape_constraints` (`_grade_graph_context`) and
   `grade_graph_validate.within_violations` (`_context`) must call ONE shared
   function that returns the centerlines, per-letter caps, spine membership and
   edges.  Same membership, same caps, same pairs — solver and validator
   identical by construction (docs/single_grade_graph.md is the intended design;
   it is not actually wired for the route-profile path).

3. **One anchor rule (runway is the truth).**  Every node that coincides with /
   is grade-graph-adjacent to a runway must be a runway CONTACT at the LOCAL
   runway elevation, so the spine grades to the runway within cap at EVERY join
   (generalises the F/14R threshold fix).  Where a building floor would lift the
   spine above the local runway, the building yields (user hierarchy), not the
   runway.  Today only the contacts `_runway_route_contacts` computed are
   honored; runway-junction shared vertices are missed.

VERIFY at each step with `/tmp/spine_v.py` (target spine=0) and `/tmp/js_root.py`
(every flagged pair must be in the solver's set with matching emitted vs solved
z).  Gate for the user's X-Plane test: spine=0 AND nothing moves the held spine
post-solve.

After spine=0: caps as bridge edges, the body layer (band from taxi+service,
building-less apron seating), then re-cut fixtures / suite / other airports /
retire legacy passes.
