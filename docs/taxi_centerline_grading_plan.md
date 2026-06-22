# Taxi-centerline grading plan — smooth, within-grade routes on variable-width & hilly airports

Status: **in progress** (2026-06-21). Owner: handoff-ready. Read this with the
memory note `apron_spine_dem_seed_climb.md` (detailed session findings) and
`apron_spine_grade_model.md` (earlier user rulings).

## 1. The model (user rulings, authoritative)

Elevation is solved by a **feasibility-bounded, priority-ordered conformance** to
FAA/EASA grade + vertical-curve rules. Two separate ideas:

- **Feasibility route profile** = per-node band `[floor, ceiling]` of what
  elevations are *POSSIBLE* at a point, given it must reach the runway/seam hard
  anchors within grade *along the taxi route* (per-letter caps — narrow code-A/B
  taxiways may climb at 3%, C–F at 1.5%). This is a *constraint*, not a target.
- **Grading** then picks, within that band, the elevations that are **smooth**
  (grade + curvature compliant) and as close to DEM as the rules allow. It may
  drive to the **ceiling OR the floor OR anywhere between** — whatever yields a
  compliant smooth surface.

**Priority hierarchy (highest first):**
1. **Runway thresholds + tile seams** — the ONLY hard anchors (immutable).
   (Runway *interior* may flex within its FAA profile; it is not a hard anchor.)
2. **Grade + curvature compliance** — sacred. If a smooth compliant surface is
   not achievable at DEM, **override DEM** wherever needed to achieve it.
3. **Buildings** — FLAT pads, at the closest-feasible-to-DEM level;
   building↔building steps are allowed. EXCEPTION (rare/extreme only — the one
   known case is HECA): a pad may take a *small* slope when that is the minimum
   needed to keep a connecting apron within grade. Default and overwhelming norm
   is dead flat.
4. **Aprons** — grade ≤1%. Prefer DEM, but grade rules override.
5. **Taxiways/junctions** — variable per-letter grade (3% narrow / 1.5% wide),
   smooth through junctions ("invisible" rect→cap→junction transitions). These
   carry the elevation change across hilly terrain (they connect everything
   within grade — the point of the feasibility band).
6. **DEM** — preferred everywhere but overridden by 2–5.

**★ Minimal-deviation principle (key correction to prior models).** Aprons and
buildings move **only as much as grade compliance REQUIRES, and no more** — they
sit at the **closest-to-DEM** elevation inside their feasibility band, NOT at the
lowest feasible / runway-pulled level. Past models over-pulled the whole complex
metres below terrain (the CYXY "bowl"); the objective is *minimise |elev − DEM|*
subject to the grade/curvature constraints and the hierarchy, so a surface that
*can* sit at DEM does, and only the spots that genuinely cannot deviate — by the
least amount. This applies to the taxi centerlines too (§5 P3): within
`[floor, ceiling]` choose the smooth profile closest to DEM, climbing/dropping
only as grade forces.

**Invariant:** every taxiway centerline route is smooth and within grade at all
times (each spine node within grade of its neighbours along the route), and
junctions are visually seamless with the rects/caps they connect.

## 2. What already works (do not regress)

The shipped solver gets very good results at most airports (HECA, SPJC, SPLP,
KPHL, OMAA, …). The corridor-profile machinery
(`_taxi_corridor_profiles`, unified_jacobi.py) already grades chains of taxi
**rects through junctions** as one smooth 1-D profile and holds them so
neighbours conform — this is the mechanism that makes junctions invisible.
**Gaps it does NOT yet handle (this plan):**
- (a) **Variable-width grade** — it caps at the uniform 1.5%, so narrow code-A/B
  taxiways can't use their 3% to climb hilly terrain.
- (b) **Hilly terrain (CYXY)** — where the network must climb ~10–14 m from the
  runway to rim buildings, the surface settles in a "bowl" ~10 m below terrain
  because nothing drives the centerlines up to their feasible ceiling.
- (c) **Some junctions not smooth** — promoted-apron corridor stretches (a taxi
  centerline running through an apron, e.g. CYXY taxiway G) are NOT chained by
  the corridor profile, so they trough/step.

## 3. Done & banked (clean, default ON, zero net-new test failures)

These are the variable-grade primitives — keep them:

- **`TAXI_REACH_BAND_BY_WIDTH`** — `_runway_reach_bands` now uses **per-edge
  caps** (`TaxiRouteGraph.edge_cap`, from `apt_taxi_letters` via
  `taxi_grade_cap_for_letter`). The feasibility band ceiling/floor reflect the
  real per-letter climb rate. (config.py; taxi_routing.py; unified_jacobi.py
  `_runway_reach_bands`.)
- **`JUNCTION_NARROW_GRADE`** (per-axis) — a junction edge running *along* a
  code-A/B centerline earns the 3% cap; ring/transverse stay 1.5% (matches the
  validator's per-axis cL=0.03/cT=0.02). `_collect_junction_axes` →
  `[(axis,cap)]`, `_edge_narrow_cap` in `_build_edges`. PER-AXIS only —
  isotropic 3% destabilises (CYXY within 18→41).

Both are NEUTRAL on the good airports (full suite 6 failed = pre-existing
baseline) and are the correct foundation for (a).

## 4. Experimental scaffolding (gated; to be SUBSUMED or RETIRED)

Built this session to prove mechanisms; **not** the final architecture. Each is
gated so the default tree stays at the §3 clean baseline.

| Flag (env / config) | What it does | Disposition |
|---|---|---|
| `O4_APRON_FEASIBLE_LIFT` (`APRON_FEASIBLE_LIFT`, default ON) | Hard-anchors each apron flat at its route-band ceiling → lifts the complex; G stepped. `_anchor_aprons_at_feasible_high`. | **RETIRE** — flat hard-anchor pins the apron body high and lets the centerline trough; wrong driver. Replace with centerline-driven conformance (§5). |
| `O4_TAXI_SPINE` (default OFF) | Grades each centerline segment independently to its cap-eroded ceiling + holds it; leaves the corridor band free. Inside `_anchor_aprons_at_feasible_high`. | **MERGE into the corridor profile** — proved the centerline can be made smooth (G 705→718) but per-segment & not network-consistent. |
| `O4_APRON_NOANCHOR` (default ON when spine on) | With spine on, skip the flat apron anchor so aprons conform up to the held centerlines. | Folds into §5 (aprons always conform). |
| `O4_BUILDING_DEM_ANCHOR` (`BUILDING_DEM_ANCHOR`, default OFF) | Hard-anchors buildings flat at feasible-DEM. `_anchor_buildings_at_feasible_dem`. | **RETIRE** — rigid pins force 410 violations (network can't reach pinned pads). Buildings should be flat-but-conforming (priority 3), not hard. |
| `_relax_buildings_and_resolve` (building-flex) | Drops anchored pads adjacent to violations. | **RETIRE** with the hard anchors. |
| `O4_DEM_ATTR` / `O4_DEM_FLOOR_ATTR` | Make the existing DEM-attraction strengths env-tunable (defaults unchanged). | Keep as tuning knobs. |

Key proven findings behind these dispositions (full detail in the memory note):
- Soft DEM attraction is **fully overridden** by the cap-projection → the weight
  must live *in* the constraint solve / cascade, not a post-hoc spring.
- The **bowl** is because nothing drives the centerlines up; once the centerlines
  are held high+smooth, aprons conform up to them (cross-shape steps 127→5).
- Wide flat aprons **cannot** grade the terrain rise across their width — only
  narrow corridors climb; where a climbing corridor diverges from a wide apron,
  an **explicit transition** is required (priority-2 steepness sink; user's
  "ramp/wall, never the apron interior" ruling).

## 5. The plan (make the corridor profile the single smooth-centerline driver)

Goal: grade the **whole centerline network** as one connected, smooth,
within-grade, FAA/EASA-curvature-compliant system, *bounded by the per-letter
feasibility band*, then conform aprons/buildings up/down to it. This subsumes
the lift (centerlines carry the climb) and guarantees the smoothness invariant.

**P1 — Per-letter feasibility band as the corridor bound. [primitive done §3]**
Compute `[floor, ceiling]` per centerline node from `_runway_reach_bands`
(per-edge caps). The corridor solve is constrained to this band; DEM is the
preference target inside it.

**P2 — Extend corridor chains to cover ALL routes. [DONE 2026-06-22 — gate
`CORRIDOR_SPINE_CHAINS` default ON]**
`_taxi_corridor_profiles` chains taxi *rects* through junctions. Extend chain
continuation through **promoted-apron junction stretches** (a centerline running
as junctions through an apron — CYXY taxiway G is 7 such segments) and any
centerline the rect-chain misses, so every `apt_taxi_centerlines` route is one
continuous profile. Use the spine-node identification already prototyped
(STRtree, nodes within ~2 m of the centerline, ordered by projection) to feed
chain stations where there is no rect.
*Implemented:* after the rect chains are built (before STAGE B, so the spine
junctions enter `j_sts` → band-exempt), for each non-SVC centerline with ≥1 node
no rect station covers, append a **station-only** `chain_data` entry
(`chain=[]`, `mouth_st=[]`, `gaps=[]`) over its spine nodes, capped per-letter.
Under `NETWORK_PROFILE_MODEL` `_network_field_stations` anchors each spine
station at the already-solved field value; first-writer-wins in `_write` means
the spine fills only the uncovered (promoted-apron) nodes and they are held
(`held_out`). A fully rect-covered centerline is skipped → airports without such
stretches stay byte-identical (gate-off ≡ gate-on there).
*Result (CYXY, seed 0):* G is one continuous smooth monotonic profile across its
35 promoted pieces (held centerline nodes 82→385); within-shape test-mirror
9→0; build validator 15→6; **`test_pavement_grade[CYXY]` flips RED→GREEN**
(suite 6→5 failed, no new reds; gate-off = the 6-failed baseline exactly).
G still tops out ~708 m vs the ~718 m rim — the field "bowl"; that climb is P3
(objective = closest-to-DEM within the band), not a P2 coverage concern.

**P3 — Per-letter caps + curvature in the corridor solve. [BAND FIX DONE &
BANKED 2026-06-22 — gate `FIELD_ROUTE_BAND_BY_WIDTH` default OFF; needs P4 to
enable]**
The 1-D profile solve must use the per-letter grade cap (3% narrow / 1.5% wide)
AND the vertical-curve / grade-change cap (`TAXIWAY_MAX_GRADE_CHANGE_PER_M`,
already referenced). Solve network-consistent (shared junction nodes are common
variables — the function already documents this for rect-chains; ensure it holds
for the extended chains). **Objective = closest-to-DEM within
`[floor, ceiling]`** (the minimal-deviation principle, §1): the profile tracks
the DEM and deviates only where the grade/curvature cap forces it, climbing or
dropping by the *least* amount needed to stay within grade of its neighbours and
ends — NOT pinned to the ceiling, NOT pulled to the floor.

*Root cause of the bowl (diagnosed 2026-06-22):* the field already seeds at DEM
and projects (closest-to-DEM), and the field-graph band honours the per-letter
cap (narrow_lines stretch edge length). BUT the `FIELD_RUNWAY_ROUTE_BANDS`
override (`_runway_route_band`, over the plain `rw_route_graph`) recomputed the
band at the UNIFORM 1.5% and REPLACED the field-graph band where it reached —
clipping a narrow code-A/B route's ceiling to 1.5% (CYXY G ceiling 727→712,
~6 m below the DEM rim → G clamped in the bowl).
*Fix (banked):* `_runway_route_band` now consumes `TaxiRouteGraph.edge_cap`
(the same 3% per-edge data `_runway_reach_bands` uses), gate
`FIELD_ROUTE_BAND_BY_WIDTH`. G's far-end ceiling 711.8→714.1 and it climbs to it.
*Why gated OFF:* standalone the loosened ceiling REGRESSES — the held centerline
climbs ~2-3 m higher while its apron/junction neighbours stay at the lower
DEM/relief level, so within-shape grade across those junctions spikes (CYXY
test-mirror within 0→10, build 6→14, a new 8.8% junction). **The climb must be
absorbed by conforming neighbours = P4.** P3's band fix and P4's conformance
must land TOGETHER; flip the gate ON with P4. (Note: G's reachable ceiling is
~714, not the full 718 rim — the route to G is mostly WIDE 1.5% taxiway, so 718
is not reachable within grade along it; the rim buildings will sit at the
closest-feasible level per the priority model, P4.)

**P4 — Aprons & buildings CONFORM to the held centerlines (no hard anchors).**
Remove the flat apron/building hard-anchors. After the corridor network is held,
apply the minimal-deviation principle (§1) — each stays as close to DEM as grade
allows, moving only where REQUIRED:
- Aprons: sit at DEM, grading ≤1%; deviate from DEM only where the ≤1% cap or
  the within-grade tie to a held centerline forces it (lift to meet a lifted
  centerline, drop only the minimum). NOT pulled to the runway-feasible floor.
- Buildings: FLAT at the closest-to-DEM level that keeps the connecting aprons
  within grade; building↔building steps allowed. EXCEPTION (rare/extreme, e.g.
  HECA): allow a *small* pad slope only when a flat pad would force a connecting
  apron out of grade — the least slope that restores compliance.

**P5 — Junction smoothness (kill the trough).**
The held centerline IS the junction's spine. Ensure the junction body grades
*to* the held spine (edges conform down/up to the centerline), never the spine
sinking below the edges. Reconcile the per-axis junction grading (§3
`JUNCTION_NARROW_GRADE`) with the held profile so they agree.

**P6 — Explicit corridor↔wide-apron transitions.**
Where a climbing corridor diverges from a wide apron that physically cannot
grade up to it (terrain too wide for 1%), emit an explicit transition
(ramp/retaining edge) that takes the height difference — never absorb it in the
apron/taxi interior. (Likely rare; CYXY G is mostly narrow side-connections, so
validate whether this is needed there before building it.)

**P7 — Retire the scaffolding (§4) once P2–P5 subsume it.** Delete the flat
apron-lift, building hard-anchor, building-flex, and standalone spine pass; keep
`TAXI_REACH_BAND_BY_WIDTH` + `JUNCTION_NARROW_GRADE`.

## 6. Validation / done-criteria

- **Centerline smoothness check (new):** sample every `apt_taxi_centerlines`
  route node-accurately (probe `/tmp/probe_spine.py` pattern: nearest emitted
  vertex along the line); assert each consecutive pair is within the per-letter
  cap and the curvature cap. NO step/trough. This is the invariant — consider a
  test.
- **Junction invisibility:** cross-shape step check (`tools/check_grade.py`
  vertex/mid-edge steps) ≈ 0 across rect→cap→junction.
- **CYXY:** taxiway G climbs smoothly E→buildings at ≤3% (no flat-start, no
  bump); G's side-connectors climb to meet it; aprons conform; buildings flat.
- **No regression** at the good airports: full suite `PYTHONHASHSEED=0
  venv/bin/python -m pytest tests/ -q` returns to the pre-session baseline
  (was 6 failed = the standing reds before this work; SPJC/SPLP compare-target
  will need re-cut once CYXY is right — user will do that).
- ⚠ **Pin `PYTHONHASHSEED=0`** for all A/B — the apron/junction partition is
  hashseed-nondeterministic (CYXY within-shape flakes 5↔18 on the same config).

## 7. Probes & tools

- `/tmp/probe_spine.py` — node-accurate centerline profile (dist-from-E, alt).
  The shape-level probe (`/tmp/probe_gdem.py`) MISREADS — it samples the flat
  apron body, not the centerline; use the node-accurate one.
- `/tmp/probe_clean.py` — within/cross/steps via `check_grade.run_checks` with
  per-axis `taxi_axes_ll` + `route_ctx` (mirrors `test_pavement_grade`).
- `O4_SPINE_DEBUG=1` — per-centerline (proj, ceiling, profile) dump.
- `O4_BAND_KML=/path.kml` — per-node `[lo,hi]` band + provenance for in-sim view.
- Build CYXY standalone: see auto_patch/CLAUDE.md (≈60–90 s). Restart Ortho4XP
  after edits (it caches `auto_patch` modules).

## 8. Files touched this session

- `config.py` — gates `TAXI_REACH_BAND_BY_WIDTH`, `JUNCTION_NARROW_GRADE`,
  `APRON_FEASIBLE_LIFT`, `BUILDING_DEM_ANCHOR`, `CORRIDOR_SPINE_CHAINS` (P2),
  `FIELD_ROUTE_BAND_BY_WIDTH` (P3, default OFF).
- `elevation_per_surface/unified_jacobi.py` — P2 spine-station chains in
  `_taxi_corridor_profiles` (inserted between the ≥2-station filter and STAGE B).
- `network_profile.py` — P3: `_runway_route_band` consumes per-edge caps
  (`graph.edge_cap`) when `FIELD_ROUTE_BAND_BY_WIDTH` is on.
- `taxi_routing.py` — `TaxiRouteGraph.edge_cap` + `_ekey`; per-letter caps in
  `build_taxi_route_graph`.
- `layout.py` — (reverted to original `taxi_shape_code_letter`).
- `elevation_per_surface/unified_jacobi.py` — `_runway_reach_bands` per-edge
  caps; `_collect_junction_axes`→caps + `_edge_narrow_cap` (per-axis junction);
  `_anchor_aprons_at_feasible_high` (spine + flat-apron, to retire/merge);
  `_anchor_buildings_at_feasible_dem` + `_relax_buildings_and_resolve` (to
  retire); env-tunable DEM attraction.
- `elevation_per_surface/junction_spine.py` — `SPINE_PIECE_ROLE_REEVAL`
  promotion (narrow spine pieces apron→junction; this one is a keeper).
