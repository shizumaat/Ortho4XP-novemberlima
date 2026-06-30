# Single grade graph — ONE within-shape constraint set for solver AND validator

> ✅ **DONE / RELOCATED (2026-06-30 audit).** The single-grade-graph model is live and
> the only path (the `SINGLE_GRADE_GRAPH` gate was retired byte-identically in `69c087e`).
> The module names prescribed here (`grade_graph_solve.py`, `spine_carries_climb_solve`)
> were never created — the solve lives in `route_profile`/`grade_graph`. Kept as design
> rationale, not as a pending plan.

Status: **in progress** (2026-06-23). Owner: handoff-ready. THE authoritative plan
for the current generation of the elevation solver. Read with
`memory/p5_lockstep_diagnosis.md`. This SUPERSEDES the P5/P6 sketch in
`docs/taxi_centerline_grading_plan.md` §9 for the connecting-geometry solve.

> ⚠ **Why this doc exists.** We have made many attempts at airside grading, each
> leaving gated scaffolding (different graphs, objectives, validators). The current
> generation collapses that to a **single graph** + a **clean-room** implementation
> (new files, wired in, old paths retired). Do NOT extend the old per-axis /
> `_visible_grade_edges` / `_min_grade_network_solve` code — build in the new module
> and delete the old once it lands.

---

## 1. The model (user, authoritative 2026-06-23)

**Two graphs, both legitimate, DIFFERENT jobs — never collapse into each other:**

1. **Taxi route graph** (`taxi_routing`) → route distance to runway thresholds →
   per-node **feasibility bands** + the **true optimal building elevations**. The
   "where can elevations be" layer. KEEP IT.
2. **Within-shape grading graph** (visibility/geodesic chords) → grades the
   **connecting surface** between the locked anchors. The "is the realized surface
   compliant" layer. THIS is the one that must become single (solver = validator).

**Objective hierarchy:**
- **Hard anchors** = runway thresholds + tile seams ONLY.
- **Buildings** = closest-to-DEM within their route-feasibility band, **then LOCKED**
  (become anchors). *Closest-to-DEM applies to buildings only.*
- **Everything else** = **minimum grade + curvature**, spread throughout, subject to
  per-edge caps + feasibility bands.

**★ No genuine infeasibility.** Every airport has a feasible solution. DEM is a
guide; runway thresholds are the anchors; we build a feasible model from data +
rules. Anything that looks infeasible is a **bug** (mis-measurement / solver / data)
— NOT a case for an "explicit transition / P6". Do not add P6.

## 2. Shape grading rules (the single within-shape graph)

Every soft airside shape is **spine + body** (this UNIFIES apron and junction —
they are the SAME code path, differing only in the body cap):

- **APRON:** spine = taxi centerline(s) through it (smooth taxi profile at the
  taxiway cap); body = visibility/geodesic chords from body nodes to the spine and
  edges, clamped to **1%**.
- **JUNCTION:** identical, except the body cap is the **taxiway-size cap** (per-ICAO
  letter: A/B 3%, C–F 1.5%), NOT 1%.
  - **With spine(s) (most junctions):** each spine is graded as a **smooth profile
    from entry to exit, like a crossing runway** — its own smooth longitudinal grade
    at the taxiway cap; two spines **share the elevation at their crossing node**
    (one canonical node); each spine **grades smoothly into the adjacent taxiway
    corridor at its endpoints** (the spine sub-graph and the corridor route-profile
    must be the SAME profile through the shared end node — a likely seam, watch it).
  - **No spine:** visibility/geodesic with the cap **inherited from the nearest
    connected taxiway-sized shape** (junctions are always wired into the taxi
    network → there is always one to borrow from).
- **RECT** (runway / sized taxiway / stub / cross_connector / parallels): the clean
  4-corner **plane** model (flat cross-ends, axial slope at the per-letter cap) +
  planar end-caps. A correct planar rect already satisfies the convex all-pair check,
  so rects are not a lockstep gap — keep the plane model.
- **BUILDING (terminal pad):** FLAT rigid group; locked at its route-feasible,
  closest-to-DEM level.
- **RUNWAY / groundside:** runway = FAA profile (hard); groundside = DEM-following
  (not solved). The validator may CHECK these; the solver does not SOLVE them — this
  is a legitimate solver/validator scope difference, NOT graph drift.

## 3. Why we are here (the diagnosis — see memory for the numbers)

The within-shape graph has **two divergent implementations** that drifted:
- solver `unified_jacobi._build_shape_constraints`/`_visible_grade_edges` (only the
  solver calls it; junction chords vs the airside UNION; per-axis diagonal-skip).
- validator `check_grade.iter_shape_grade_constraints`/`_polygon_visibility`
  (consumed by `check_grade` + the audit tool; chords vs the shape's OWN ring;
  per-axis diagonal-skip → **junction bodies largely UNGRADED**).

On CYXY (full stack) the validator flags **481** within violations; **146** are on
chords the solver never graded; a direct constraint-set diff is 11352 (solver) vs
18051 (validator). The solver cannot fix what it doesn't grade → 481 can't reach 0
while two graphs exist.

**Enabler:** the pre-solve geometry refactor is COMPLETE
(`docs/presolve_geometry_refactor.md`) — airside geometry is final before the solve
(Phase-8 guard = 0 on HECA), so one generator on the shared geometry yields
identical pairs by construction. (⚠ CYXY still drifts 5 airside shapes post-solve →
Phase 0.)

## 4. Clean-room implementation (new files, then wire in, then retire old)

**New module `src/auto_patch/grade_graph.py`** — THE single within-shape grading
graph. Pure, self-contained, geometry-representation-agnostic. Both the solver
(pre-emit `layout.shapes`) and the validator (post-emit OSM ways) build the same
lightweight input and call it → identical constraints by construction.

Input (built by each caller from its own representation):
```
GradeShape:                 # one soft airside shape
    role: str               # apron | junction | <rect roles> | building
    ring: [(x, y), ...]     # open ring, LOCAL meter coords
    keys: [hashable, ...]   # stable per-vertex key (OSM nid | solver node-idx)
    spine: [[key, ...], ...] # ordered spine node-key chains through the shape
                             # (from junction_spine slicing); [] if none
    cap: float | None        # resolved body cap (None → resolve via inheritance)
GradeContext:
    taxi_axes:  centerlines + per-letter (cL, cT)   # for spine grading
    seam_keys:  set                                  # seam-anchored → drop pair
    airside_union: prepared geom                     # visibility container
    road_zone / frontage_keys: relaxations
    cap_for_letter(letter) -> grade ; nearest_taxi_cap(shape) -> grade
```
Output: per-shape constraint record (nodes, body edges `(a,b,cap)`, spine chains +
cap, flat/rect-plane info) + a `flatten_pairs()` helper → `(key_a, key_b, cap,
allowance)` for the validator. ★ ONE place encodes: visibility/geodesic gate, the
spine+body split, cap resolution + inheritance, seam drop, the road/ramp
relaxations. No second copy.

**`src/auto_patch/grade_graph.py` + hermetic unit tests** are written and verified
in isolation FIRST (fast), before any wiring.

Then wire, each step behind a gate for A/B, deleting the old path once green:
- **Phase 0** — close CYXY's post-solve geometry drift (`O4_GEOM_GUARD=1`).
  **★ Key insight:** the grade graph is **altitude-independent** — which pairs are
  graded + at what cap depends only on XY + role + spine, never on altitudes. So
  any ALTITUDE-only post-solve pass is lockstep-safe and may stay. Of CYXY's 5
  post-solve airside changes: `debulge_cap_centre_nodes` + `_smooth_junction_ring_
  curvature` are **altitude-only** (no XY change → not even flagged by the
  geom-guard, which hashes XY; they were never the issue). The real XY-geometry
  passes were `_dedup_coincident_ring_vertices` (grade-neutral anyway — grade_graph
  filters coincident via `_MIN_PAIR_DIST_M`) and `drop_flatedge_nodes` (drops a real
  apron vertex — the one that matters). **DONE:** both moved PRE-solve (gate
  `O4_PRESOLVE_CLEAN`, default ON; pipeline.py after `_unify_airside_geometry`,
  before the guard snapshot; idempotent post-solve copies kept). CYXY geom-guard
  **5 → 2**; the residual 2 are `_insert_bridge_contacts_into_junctions` — the
  documented Phase-5 solve-dependent exception (bridge placement needs solved
  altitudes), and they are **collinear** inserts → grade-neutral (a subdivided edge
  complies iff the original does), so they do not break grade-graph lockstep.
- **Phase 1** — validator consumes `grade_graph` (build GradeShapes from OSM).
  Decide junction semantics here (see §5). Re-cut fixtures for the intended change.
- **Phase 2 [DONE]** — solver consumes `grade_graph` for apron/junction (gate
  `O4_SINGLE_GRADE_GRAPH`/`SINGLE_GRADE_GRAPH`, default OFF; helpers
  `_grade_graph_context` + `_grade_graph_edges` in unified_jacobi; ROLE_BUILDING +
  service_junction stay legacy). Gate-off byte-identical. **Measured (CYXY, full
  airside stack, gate ON): apron/junction within-violations = 348** under the
  unified graph (`/tmp/probe_sgg_within.py`). The graph is now consistent; 348 is
  the OLD solve's quality gap (aprons 10%/4–15 m, junctions ~9%) → Phase 3 clears
  it. NOT infeasibility (the old solve fighting over-pinned P4 buildings on a now-
  correct graph).
- **Phase 3** — the connecting solve (NEW, e.g. `grade_graph_solve.py` or a clean
  fn): lock buildings (closest-to-DEM in route band) + runway + seams, then solve
  free nodes to **min grade + curvature** on the one graph (bands = direct Dijkstra
  from anchors; smooth assignment = bounded sweep — NO 60k-iter POCS). Replaces
  `_min_grade_network_solve`.
  **[WIP, BUILT — commit 92db144]** `grade_graph_solve.connecting_solve`, wired when
  `O4_SINGLE_GRADE_GRAPH` is on. ★ **KEY FIX:** buildings are locked on the
  CONNECTING graph's OWN bands (closest-to-DEM within each pad's band-intersection),
  NOT pre-pinned from the route graph — pre-pinning (P4) seated pads at levels the
  within-shape graph can't deliver → 443 nodes with floor>ceiling (FALSE
  infeasibility). Locking on the connecting bands → floor>ceiling **443→1**, and
  CYXY apron/junction within **481→69**, with EVERY APRON resolved (residual = 69
  junctions only). The route graph (P4) over-measured the building reach; the
  connecting-graph band is the consistent one. **REMAINING (the 69):** all
  junctions. **[CONVERGED — commit e355e61]** Two further fixes took it to **481→4**:
  (1) **projected Gauss-Seidel** — each free node lands directly in its cap-feasible
  interval (neighbour slabs ∩ band) pulled toward the min grade+curvature target;
  monotone/convergent (102 sweeps; in-solve residual 8 edges = 7 both-hard + 1
  has-free). (2) **auto-disable the legacy post-solve altitude band-aids** under the
  gate — `_smooth_junction_ring_curvature` (elevation.py) + `debulge_cap_centre_nodes`
  (pipeline.py) were re-introducing ~66 junction violations by fighting the
  connecting solve (they were built for the old graph). Result: **CYXY apron/junction
  within = 4** (mild ~4% apron spots next to locked buildings) + 7 in-solve both-hard
  edges = the final cleanup. The single-graph stack is now just
  `O4_SINGLE_GRADE_GRAPH=1 O4_UNNAMED_TAXI_SIZE=1 O4_FIELD_ROUTE_BAND_BY_WIDTH=1`
  (building lock is done by the connecting solve, so P4 `BUILDING_ROUTE_FEASIBILITY`
  is no longer needed).
  **[CYXY → 0 — commit 4470971]** The final 4+7 were the inter-pad frontage: an
  apron/junction edge with BOTH endpoints on building pads is a building↔building
  step (allowed), so it is EXEMPT (`GradeContext.building_keys`; mirrors the
  validator step exemption). CYXY apron/junction within **4 → 0**.
  **MULTI-AIRPORT (the architecture is proven on CYXY; the others need Phase 3b):**
  SPJC 211→134, HECA 1034→928. Dominant remaining = **INFEASIBLE BANDS** (HECA 2593,
  SPJC 148 floor>ceiling): pads locked INDEPENDENTLY (each closest-to-DEM in its
  runway-reach band) → on a canyon (HECA terminal 82–88 m) adjacent pads sit at
  mutually-incompatible levels and the free apron node between can't grade ≤1% to
  BOTH → empty band.
- **⚠⚠ PHASE 3 CORRECTION (2026-06-23) — the "within 0" was a BOWL = FAILURE.**
  Locking buildings on the CONNECTING-graph band (above) DEVIATES from §1 ("buildings
  = closest-to-DEM within their ROUTE-feasibility band, then LOCKED") and bowls them:
  CYXY building5 ends at 707.7 vs DEM 713.06 (−5.36 m). A within=0 reached by bowling
  the gate area FLAT-and-LOW is a FAILURE against the central pillar (buildings at DEM
  + taxiways carrying the climb). ROOT of my wrong shortcut: the "443 false-
  infeasibility" from pre-pinning at the route level was a BAND-SOURCE BUG — bands
  were computed from runway+seam ONLY, EXCLUDING the locked buildings, so a node next
  to a building couldn't see it as a source → false floor>ceiling. **CORRECT FIX
  (§1, restored):** lock each building at its ROUTE-feasibility closest-to-DEM level
  (`building_feasibility.building_feasible_levels`; building5 → ~713) as a HARD ANCHOR
  *and* a BAND SOURCE; then the connecting solve grades the taxiway/arm UP to it. No
  pass-1 connecting-band clamp on buildings.
  **★ DONE-CRITERION (makes bowling un-claimable as success):** every building must
  emit at ≈ `min(DEM, route_band_ceiling)`; a build where a building sits materially
  below that is a FAILURE even if within=0. Add a test.
- **★ SPINE-CARRIES-CLIMB — the refined design (2026-06-23, decisive diagnosis).**
  The bowling/regression is because the **connecting solve does the ROUTE graph's
  job**. Its Dijkstra bands route a building's reach through the CHEAPEST (1% apron)
  or SHORTEST (to a nearby low node) connecting-graph path, which UNDER-measures the
  real reach and manufactures false infeasibility. Decisive CYXY finding: building8's
  route-feasible level is **712.4** (`building_feasible_levels`, reachable via the
  long taxi route), but the connecting graph finds a SHORT path to a nearby 694
  runway-level node → declares it infeasible (462 nodes floor>ceiling; worst pair
  `bld:building8 712.4 ↔ runway 694`). Locking buildings at route levels then
  regresses (744 residual). Conversely, locking on the connecting band BOWLS
  (building5 707.7 vs DEM 713).
  **THE DESIGN (two graphs, per §1):**
  1. **ROUTE graph carries the climb.** Building levels = `building_feasible_levels`
     (route-feasible closest-to-DEM). The SPINE (taxi rects + centerline spines
     through junctions/aprons) gets a **climbing profile** computed on the ROUTE
     graph (cap-weighted band from runway+seam over the taxi network at per-letter
     caps) — NOT the connecting graph. The spine climbs runway→building over the
     real taxi route; impose it as soft anchors.
  2. **CONNECTING graph grades the apron body ≤1% from its LOCAL SPINE** (not from
     spurious short/global paths to far-low anchors). An apron node's band floor/
     ceiling come from its spine, which is at the climbing level.
  3. Where the connecting graph has a spurious short path (building on high terrain
     near a low runway), that is NOT a real grade path — the climb is the taxiway's
     (long route); do not let it bowl/infeasible the building.
  KEY: the connecting-graph global Dijkstra is the WRONG tool for building/spine
  reach — that is the ROUTE graph's job. This is substantial (route-graph cap-band +
  spine-profile impose + apron-from-spine) and should be built behind its own gate,
  measured, and only defaulted on when buildings sit at route levels with within=0.
- **Phase 3b — JOINT BUILDING FEASIBILITY (the canyon case, NEXT).** Lock pads at
  MUTUALLY-consistent levels, not pad-by-pad — the connecting surface between two
  pads must be gradeable: apron ≤1% to its local SPINE; the spine carries the climb
  at the taxi cap; a large apron is spine-sliced so each piece grades ≤1% locally;
  where a pad genuinely cannot co-level it STEPS and the apron follows it (building
  ↔building step, already exempt). This couples the per-pad band lock through the
  connecting graph (a pad's lock must respect the already-locked neighbours within
  cap·dist). Probe: `O4_PROBE_ICAO=<icao> /tmp/probe_sgg_within.py`.
- **Phase 4** — verify + land: CYXY within → 0, climb preserved, buildings at
  route-feasible/closest-to-DEM; no net-new suite regressions; flip airside gates
  ON; user re-cuts SPJC/SPLP; add centerline-smoothness + buildings-closest-to-DEM
  tests; **delete** the retired scaffolding (old per-axis junction model,
  `_visible_grade_edges`, `_min_grade_network_solve`, the P3a/P3/P4 gates folded in).

## 4b. COLLAPSED TO ONE PATH (2026-06-23) — spine-carries-climb is THE solve

The Phase-3 bowling `connecting_solve` is **DELETED**. There is now exactly ONE
apron/junction grading path under `SINGLE_GRADE_GRAPH` (default ON, no sub-gate):
`grade_graph_solve.spine_carries_climb_solve`. The earlier `O4_SPINE_CARRIES_CLIMB`
sub-gate and the `connecting_solve`/`_bands`/`_dijkstra_envelope` scaffolding are
gone (clean-room rule: one thing grades aprons).

**The model as built (CYXY):**
1. **HARD anchors** = runway thresholds (FAA surface, unchanged) + tile seams +
   route-feasible closest-to-DEM **buildings** (`building_feasible_levels`).
2. **SPINE = its route-traced climbing profile, LOCKED.** `_spine_climb_seats`
   (unified_jacobi) builds the taxi-centerline sub-graph and picks, within the
   existing runway-reach band (`_runway_reach_bands`), the in-band assignment that
   climbs at the per-letter cap (closest-to-DEM, 1-D projected GS). ★ KEY INSIGHT
   (user 2026-06-23): the per-node feasibility band is **reachability**, not a
   pairwise grade constraint — two in-band nodes can still step more than the cap
   because DEM steps inside the band. Only the **climbing profile** (the specific
   in-band assignment that rises at the cap along the route) is pairwise-compliant
   by construction. So the spine MUST be set to that profile and locked, not left
   to free min-grade (which picks a stepped in-band assignment → the 60 spine
   violations). Locking it → **SPINE violations 60 → 8** (the 8 are building-
   coincident frontage nodes = canyon, not the route).
3. **BODY** = the apron/junction interior, solved to min Σgrade² off the locked
   spine + buildings (`spine_carries_climb_solve`: harmonic + projected Gauss-Seidel
   cap-slab, no global band). Buildings emit at route level (**0 bowled**).
4. **grade graph** drops a body↔body chord that CROSSES the shape's spine (the
   real path is via the spine at the taxi cap; the straight 1% diagonal across a
   wide apron is not a grade path) — now unconditional in `grade_graph`.

**As-built validation is unified.** `grade_graph_validate.within_violations(layout)`
builds the SAME `grade_graph` constraints from the emitted geometry; the build's
WARN reports it, split **SPINE(taxi-route) vs BODY(apron)**. No parallel probe.
(⚠ `tools/check_grade.py` — the test-suite validator — is STILL legacy; wiring it
to `grade_graph` + re-cutting fixtures is the remaining Phase-1 chore.)

**Where it stands:** spine clean (8 frontage residuals); **BODY ≈ 1284** = the
canyon — locking the spine at its correct climbing height exposes that wide aprons
between stepped pads cannot grade ≤1% to the high spine/buildings. That is the
**NEXT** task (§3b): spine-slice wide aprons / joint pad feasibility so the body
grades ≤1% to its LOCAL spine, with building↔building steps where pads can't
co-level. Probe/visuals: `/tmp/viz_violations_png.py`, `/tmp/viz_violations_kml.py`.

## 5. Junction model — how it differs from what's implemented (resolve in Phase 1)
- **Body grading is the gap.** Current `_per_axis_allowance` requires BOTH endpoints
  within 15 m of a common centerline; a junction edge node beyond that → pair
  RETURNS `None` → **skipped** (`check_grade.py:949`). So wide-junction bodies are
  ungraded. New model: grade EVERY spine→edge visibility/geodesic chord at the
  taxiway cap (the apron treatment, at the taxiway cap).
- **Cap.** Current junction cap = uniform `ROLE_GRADE_LIMITS["junction"]` 1.5%
  (`_shape_grade` → role cap; junctions have no taxi letter). New: the spine's
  taxiway-size cap; spine-less → nearest connected taxiway cap.
- **Spine.** `junction_spine.apply_junction_centerline_spine` ALREADY slices the
  centerline into real shared nodes (keep — the enabler). New: grade the spine as an
  explicit held smooth profile continuous into the adjacent corridor (not just a
  pairwise per-axis check).
- **Drop** the per-axis allowance + diagonal-skip; apron and junction become one
  spine+body path parameterized by body cap.

## 6. Probes (/tmp; recreate; all need the 4 airside env gates + `PYTHONHASHSEED=0`)
- `probe_p5_diag.py` — classify violations (seen/unseen, hard-class, role, distance).
- `probe_constraint_diff.py` — solver-vs-validator constraint-set diff.
- `probe_unseen.py` — why each unseen edge is missing.
- `probe_lockstep_solve.py` — feasibility check (⚠ rewrite: hard = runway+seam ONLY,
  buildings soft/locked-at-band; objective = min grade+curv, NOT closest-to-DEM).

## 7. Baselines / guardrails (PIN `PYTHONHASHSEED=0`)
- `test_pavement_grade` default build (P2 baseline): HECA red (standing), CYXY/SPJC/
  SPLP green (~7 min).
- Full suite default: 5 failed / 359 passed (per STATUS) — the 5 are standing reds +
  expected compare-target shifts.
