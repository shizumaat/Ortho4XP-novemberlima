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

*Investigation 2026-06-22 — where P3's climb is lost, two dead-ends ruled out.*
Traced (with P3 ON) why the held G centerline doesn't keep its climb. The
corridor pass DOES write the field value onto G (`_write n973=708.89`, held=True)
and the post-corridor relief preserves it. The loss happens in the FINAL
within-shape enforce, in a stage tagged **`post-final`** (`_enforce_within_shape_
grade`, unified_jacobi.py ~L3318/L3455): the *"CORRIDORS YIELD TO FLAT TERMINALS"*
release — `held_yield = held_all - corridor_held_set` — deliberately RELEASES the
corridor-held set and re-projects, letting the held centerline flex down within
its band so the network pulls slack toward a flat pad BELOW it (the SPJC
building20 case). With P3's raise, that release sinks G from 708.89 back to
707.24 — even though `building3` next to it is FLAT at 709 and the field value
(708.89) already AGREES with it. So the climb is undone by the yield-release.
**Dead-end A (ruled out):** keeping the corridor HELD through the release (don't
subtract `corridor_held_set`) makes within-shape WORSE, not better (CYXY 10→13):
the apron/junction/building neighbours do NOT conform UP to the held-high
centerline — they stay pinned at their lower DEM/relief level, so a held-high
corridor just opens MORE steps against them. **Dead-end B:** the band loosening
alone (P3) — same regression (0→10). **Conclusion:** P4 is genuinely the
ACTIVE-upward-conformance problem — the apron/junction/building neighbours must
be DRIVEN UP to the held climbed centerline (removing whatever pins them low:
route-band floors, the DEM/relief seed, the flat-pad level), not merely "held or
released". This is coupled with P5 (the junction interior must grade to the held
spine). The two quick levers (release vs hold) both regress; this needs the real
conformance rework, not a toggle. The yield-release itself must become
BIDIRECTIONAL: yield the corridor DOWN to a flat pad BELOW it (SPJC), but hold it
and lift the neighbours UP when the pad/feature is ABOVE it (CYXY). All P4
attempts this session were reverted (tree clean at the P3-banked commit).

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

---

# 9. BRINGING IT TOGETHER — the closest-to-DEM conformance (the final piece)

*Authoritative 2026-06-22. This section SUPERSEDES the P4–P7 sketch above. It is
written after a full pipeline + history analysis (the four-subsystem maps and the
distilled record of every prior attempt). Read §1 (the model) first; this is how
we finally satisfy it.*

## 9.1 The one-paragraph diagnosis (why every attempt so far fell short)

The network-profile **field `F` already computes the right answer**: solved over
the whole centerline graph, seeded at DEM, projected onto the per-letter
feasibility band, with a lift-only apron-plane pass and a building chord-window —
so at CYXY it puts taxiway G's climb end at **708.9 m**, which already AGREES with
`building3` sitting flat at **709 m**. The corridor is *not* really in a bowl in
the field; **the bowl is a TRANSFER failure.** The emitted apron / junction /
building vertices are seeded at raw DEM in phase 1, flattened toward a low
compromise by `_directional_relief` (wide aprons cannot grade the terrain rise at
1 %), and then the final `_enforce_within_shape_grade` uses `F` only as *band
bounds* — never as a *target*. Its projection (`_project_within_bands`) is a
**movement-minimising POCS from that corrupted low seed with NO attractor**, so
nothing pulls the neighbours UP to the held corridor; and the final *"CORRIDORS
YIELD TO FLAT TERMINALS"* release (`held_all − corridor_held_set`,
unified_jacobi ~L3318/L3455) then sinks the held corridor back DOWN toward those
low neighbours. **The solver does not implement the user's stated objective**
(§1: *minimise |elev − DEM| subject to grade + curvature, within the feasibility
band*). It minimises movement from a bad seed. That single mismatch is the whole
remaining problem.

This is confirmed by the fact that *every* dead-end failed for the SAME reason —
no active driver to the consistent level:

| Attempt | Result | Why it failed (all = "no driver to the consistent level") |
|---|---|---|
| Building hard-anchors (`BUILDING_DEM_ANCHOR`) | within 18→**410** | rigid pins manufacture infeasibility the network can't yield to |
| Apron flat-lift (`APRON_FEASIBLE_LIFT`) | within→**363**, steps→127 | one flat level per apron → adjacent aprons step against each other |
| Spine hard-hold (`O4_TAXI_SPINE`) | within 327→**2882** | spine pinned at raw ceiling 718; the WIDE apron can't grade 1 % up to it |
| P3 band-loosen alone | within 0→**10** | corridor ceiling rises, neighbours don't follow |
| Corridor-hold-through-enforce | within 10→**13** | corridor held high, neighbours STILL not lifted → more steps |

And by the user's repeated rulings (history): buildings FLAT at closest-feasible
DEM; aprons **1 % whenever possible**; the **taxi network carries the climb via
its route-band slack**; *"spend taxi-network slack, not steep aprons"*;
minimal-deviation = closest-to-DEM, move only as grade forces. The
`TAXI_SLACK_TERMINALS` work (merged ON) already flattens buildings at their
feasible level by this model and improved every airport — so the building side of
the model is in place. The missing half is making the **aprons / junctions /
corridor adopt the field's consistent closest-to-DEM surface** instead of the
relief bowl.

## 9.2 The principle (what to implement)

**Make the objective real.** For every soft (non-hard-anchor) node, the solved
elevation must be the **closest-to-DEM value within its feasibility band that is
network-consistent and grade-compliant** — which is exactly what the field `F`
encodes. So: **adopt `F` as the per-node TARGET of the final enforce**, not just
as band bounds. The enforce already has the band `[lo, hi]` (route-band ∩
edge-band, now per-letter with P3); give its projection a target = `F` (clamped
to the band), and POCS will settle the *fine* emitted surface on the *coarse*
field's consistent levels — corridor AND its apron/junction/building neighbours
rising together, so no step ever opens between a held corridor and its
neighbour.

Two hard-won constraints on HOW (from the history):

- **The target must live IN the solve, as the projection's clamp target each
  sweep — NOT a soft spring.** A weak DEM/field spring is "fully overridden by
  the cap projection" (the retired soft-DEM-attraction lesson). Implement it as
  the box-clamp center the POCS pulls toward, like the field's own seed-then-
  project does.
- **It must be LIFT-BIASED, not a blanket re-seed.** The bowl is too LOW; we need
  to RAISE the airside toward the field/DEM level, not disturb surfaces already at
  or above it. Mirror the field's apron-plane pass, which is deliberately
  **lift-only** (`v = max(elev, corridor − grade·dist)`). A blanket re-seed to DEM
  would churn the four good airports; a lift-only re-target toward
  `min(F, ceiling)` only undoes the bowl.

## 9.3 The sequenced plan

**P3 — flip the per-letter field route-band ON.** Already built
(`FIELD_ROUTE_BAND_BY_WIDTH`); it widens the band ceiling to the real per-letter
reach so the field/target can climb. It only regressed because P4 was missing; it
flips ON together with P4a.

**P4a — Field-target conformance (the core).** In the final enforce, before the
movement-minimising projection, **re-target every soft node toward the field**:
`target_i = F.sample(x_i, y_i)` where `F` covers it (sample gap ≤ ~a taxi-width),
else `clamp(DEM_i, lo_i, hi_i)` (closest-to-DEM where the field is silent — open
apron interiors). Apply **lift-only within band**:
`elev_i ← max(elev_i, min(target_i, hi_i))`, never below the current value, never
above the ceiling. Then run the existing `_project_within_bands` (held corridor +
hard anchors immovable) so the lifted surface is driven grade-compliant. Effect:
the apron/junction vertices around G rise from the relief bowl up to ~708–709
(the field/building level), the held G centerline (708.9) now has consistent
neighbours, and the within-shape steps close. Gate `FIELD_TARGET_CONFORMANCE`
(default OFF until validated); pairs with P3.
*Where:* `_enforce_within_shape_grade` (unified_jacobi.py ~L2300, after the band
computation and the `_term_nodes` handling, before the first `_project_within_
bands` at ~L2506). The field is `layout._network_profile_field` (already stored);
`F.sample` / `F.sample_band` exist.

**P4b — Make the yield-release bidirectional (or retire it).** With neighbours
lifted by P4a, the *"CORRIDORS YIELD TO FLAT TERMINALS"* release (L3318/L3455)
must no longer sink a corridor that should stay high. Change the release from
unconditional to **directional**: a corridor node yields DOWN only toward a
serving pad whose flat level is genuinely BELOW the corridor's feasible floor (the
SPJC building20 canyon case); when the adjacent pad/feature is at or ABOVE the
corridor (CYXY building3), the corridor stays HELD and the pad/apron conforms (it
already did, via P4a). Concretely: keep a corridor node in the held set during the
release unless `pad_level < corridor_value − cap·dist` for some served pad.
First validate whether P4a alone already neutralises the sink (lifted neighbours
give the release nothing lower to sink toward) — if so, the release can simply
keep corridors held (retire the subtraction) and SPJC must be re-checked.

**P5 — Junction smoothness falls out, then reconcile.** With P4a the junction
interior targets the field too, so the held centerline and the junction body sit
at one consistent level (no trough). Verify the per-axis junction caps
(`JUNCTION_NARROW_GRADE`) agree with the field target along the narrow axis; the
band-exempt set for corridor-touched junctions stays (so the enforce doesn't
re-pin them off the field).

**P6 — Explicit transitions only where physically forced (contingency).** Where a
WIDE apron genuinely cannot grade up to a climbing corridor at 1 % (terrain too
wide — the `O4_TAXI_SPINE` 2882 explosion is the warning sign; HECA-class, not
CYXY), the field's lift-only apron-plane already caps the apron at its reachable
level and leaves a residual; emit an explicit ramp/retaining transition for that
residual (user ruling: the steepness sink is an explicit transition, never the
apron/taxi interior). Validate whether CYXY needs it before building (G is narrow
side-connections — likely not).

**P7 — Retire scaffolding + lock it with tests.** Once P4a+P4b carry the climb,
delete the gated-off dead-ends (`APRON_FEASIBLE_LIFT` /
`_anchor_aprons_at_feasible_high`, `BUILDING_DEM_ANCHOR` /
`_anchor_buildings_at_feasible_dem` / `_relax_buildings_and_resolve`, the
standalone `O4_TAXI_SPINE` pass). Keep the keepers (`TAXI_REACH_BAND_BY_WIDTH`,
`JUNCTION_NARROW_GRADE`, `CORRIDOR_SPINE_CHAINS`, `W2_CLEAN_BANDS`, planar caps,
`TAXI_SLACK_TERMINALS`) and the new P3 + P4 once defaulted ON. Add the two tests
in §9.5.

## 9.4 Why this is different from the things that already failed

- **Not hard anchors.** The target is a *soft* clamp inside the band; where the
  network can't reach it, POCS yields — no manufactured infeasibility (the
  `BUILDING_DEM_ANCHOR` 410 failure mode cannot recur).
- **Not the flat apron-lift.** The target is a *per-node graded* field value, not
  one flat level per apron, so adjacent aprons can't step against each other (the
  363/127 failure mode cannot recur).
- **Not the raw spine-hold.** The target is the field's *feasible* apron level
  (lift-only, band-clamped), NOT the raw 718 spine ceiling — a wide apron is never
  asked to grade 1 % up to a level it cannot reach (the 2882 explosion cannot
  recur). Where it truly can't reach → P6 transition, per the ruling.
- **Respects the soft-spring lesson.** The field enters as the projection's clamp
  TARGET, not a weak additive spring, so the cap projection can't override it.

## 9.5 Validation / done-criteria (pin `PYTHONHASHSEED=0` for ALL A/B)

- **CYXY:** taxiway G one smooth ≤3 % climb from the E end to the rim; its
  side-connectors climb to meet it; the apron/junction vertices beside G sit at the
  field level (no 0.7 m junction steps); `building3` flat ≈ 709 consistent with G
  708.9; `test_pavement_grade[CYXY]` stays GREEN; `probe_clean` within = 0 with the
  climb PRESERVED (not the P2 within=0 that came from a lower G).
- **Closest-to-DEM check (NEW test):** for every emitted soft node, assert
  `lo ≤ elev ≤ hi` and that `elev` is within tolerance of `min(DEM, hi)` wherever
  the band permits DEM — i.e., the surface is not pulled needlessly below terrain
  (catches any return of the bowl).
- **Centerline-smoothness check (NEW test):** sample every `apt_taxi_centerlines`
  route node-accurately; assert each consecutive pair within the per-letter cap +
  the curvature cap; no step/trough.
- **No net-new regressions:** full suite returns to its standing-reds baseline
  (`rests_on_source[CYXY]`, `grade[HECA]`; SPJC/SPLP compare-target re-cut by the
  user once CYXY is right). **SPJC building20 (the yield-down case) is the gating
  regression check for P4b** — confirm it still flattens.

## 9.6 Risk register (carried from the distilled traps)

- **SPJC building20** — the canyon yield-DOWN case the release was built for; P4b
  must preserve it. Check first, before retiring the release.
- **HECA wide-apron canyon** — the case that may need P6; the `O4_TAXI_SPINE`
  327→2882 blow-up is the canonical warning. Watch HECA within-shape under P4a.
- **`PYTHONHASHSEED=0`** for every A/B — the apron/junction partition is
  hashseed-nondeterministic (within flakes 5↔18 on the same config).
- **DEM smoothing pix** — standalone reads repo `Ortho4XP.cfg` (`apt_smoothing_pix`
  = 4 here, 8 on dev); production passes the pre-smoothed tile_dem. Standalone ≠
  production when this differs — suspect DEM/config before geometry.
- **Plain vs augmented route graph** — the field route-band uses the PLAIN
  `shared_taxi_route_graph` deliberately (no taxiing-the-runway shortcut); keep it.
- **Identify shapes by COORDINATE, not index/ref** (indices drift across builds);
  measure A/B in a detached worktree if another session may commit concurrently.
- **`F.sample` coverage** — in deep apron interiors the field sample gap is large;
  P4a must fall back to `clamp(DEM, lo, hi)` there, not use a far centerline value.

## 9.7 Implementation map (file:line anchors)

- Field source: `network_profile.build_and_solve` → `layout._network_profile_field`
  (`F.sample`, `F.sample_band`, lift-only apron-plane ~network_profile.py:1454–1745).
- P3 band: `network_profile._runway_route_band` per-edge `edge_cap`
  (gate `FIELD_ROUTE_BAND_BY_WIDTH`) — built.
- P4a target re-clamp: `_enforce_within_shape_grade`
  (unified_jacobi.py ~L2300 → before `_project_within_bands` ~L2506); bands `lo/hi`
  already computed there; held set = `held_all`.
- P4b release: the two `held_all − corridor_held_set` sites
  (unified_jacobi.py ~L3318, ~L3455).
- Seeding context: phase-1 DEM seed `_phase1_hop_priority` (~L4080); relief
  `_directional_relief` (~L4095, no reseed) — the bowl origin P4a corrects.
- Keepers to preserve: `TAXI_REACH_BAND_BY_WIDTH`, `JUNCTION_NARROW_GRADE`,
  `CORRIDOR_SPINE_CHAINS` (P2), `W2_CLEAN_BANDS`, planar caps, `TAXI_SLACK_TERMINALS`.
- `layout.py` — (reverted to original `taxi_shape_code_letter`).
- `elevation_per_surface/unified_jacobi.py` — `_runway_reach_bands` per-edge
  caps; `_collect_junction_axes`→caps + `_edge_narrow_cap` (per-axis junction);
  `_anchor_aprons_at_feasible_high` (spine + flat-apron, to retire/merge);
  `_anchor_buildings_at_feasible_dem` + `_relax_buildings_and_resolve` (to
  retire); env-tunable DEM attraction.
- `elevation_per_surface/junction_spine.py` — `SPINE_PIECE_ROLE_REEVAL`
  promotion (narrow spine pieces apron→junction; this one is a keeper).
