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
G's far end still sits ~708 m under P2 alone — a partial bowl vs DEM ≈711.5 there
(NOT the "~718 m rim" stated in an earlier draft; the buildings top at 709.1).
Lifting it to DEM is P3 (objective = closest-to-DEM within the band), not a P2
coverage concern.

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

## 9.1 The one-paragraph diagnosis (ROOT CAUSE, verified 2026-06-22)

The bowl has a concrete root cause — **a dropped ICAO size code** — compounded by
a building-placement that then trusts the resulting wrong band:

1. CYXY's gate **arms** (the connectors from taxiway G up to the terminal) are
   apt.dat `taxiway_A` edges — ICAO code **A → 3 % cap** — but they carry **no
   name**. `apt_dat_reader.taxi_size_letters` is keyed by name and **skips
   unnamed edges** (`if not e.name: continue`, ~L1434), so **all 9 code-A arms —
   every one reaching the 713–720 m terminal terrain — lose their 3 %** and fall
   back to the uniform 1.5 %. (Verified: the letters map is
   `{G:A, E:D, A2:B, A:D, F:D, D:D}` — not one of the 9 unnamed arms appears.)
2. So the runway-anchor **feasibility band to the terminal is computed at 1.5 %,
   half the legal climb** — its ceiling at the buildings is ~709. The
   building-flatten then seats the terminal pads at that false-low ceiling,
   **~9 m below their true DEM (≈718; building1 717.8, building3 717.9)**. G and
   the aprons settle into the resulting bowl (emitted ~707–710 vs DEM 710–718).

So the field is **NOT** "already computing the right answer" — its *inputs* (the
arm caps) are wrong, and the buildings are then placed at a falsely-low feasible
level. (An earlier draft of this section claimed the field was already correct;
that was read off the *bowled emitted* building levels — corrected here.) Two
things must both be fixed: the **caps** (so the band reflects the real 3 % arms)
and the **building placement** (closest-to-DEM within the *correct* band). And the
DEM rises ~4.4 % along G (709.9→717.2 over 321 m), STEEPER than even the 3 % cap,
so G itself tops at its ~3 % ceiling (~712–714) and the last metres up to the
buildings are the arms' job — exactly the user's "G gets to ~714, the arms take
the last climb."

The historical dead-ends are all explained by this: every one tried to lift the
airside WITHOUT first correcting the feasibility band, so it fought a band that
was wrong by ~half:

| Attempt | Result | Why it failed |
|---|---|---|
| Building hard-anchors (`BUILDING_DEM_ANCHOR`) | within 18→**410** | pinned pads at a DEM the (wrong, 1.5 %) band said was unreachable → infeasibility |
| Apron flat-lift (`APRON_FEASIBLE_LIFT`) | within→**363**, steps→127 | one flat level per apron → adjacent aprons step against each other |
| Spine hard-hold (`O4_TAXI_SPINE`) | within 327→**2882** | spine pinned at its (too-low) band ceiling; the wide apron can't grade 1 % up to it |
| P3 band-loosen alone | within 0→**10** | loosened G's ceiling but NOT the arms' / buildings' → corridor climbs, neighbours don't |
| Corridor-hold-through-enforce | within 10→**13** | held corridor high, neighbours still pinned low by the wrong band → more steps |

## 9.2 The principle (the user's model, now precise)

**Per-building route-feasibility → closest-to-DEM.** Buildings are the heaviest
anchor, seated **as close to DEM as the real taxi route can actually reach** —
not full DEM where terrain outruns the taxi cap, and never the runway-pulled
bowl. For each building, compute the feasibility band along the actual route TO it
at the *correct* per-edge per-letter caps (arms = 3 %); seat the pad flat at
`min(DEM, band_ceiling)`. Then every taxiway/arm reaches its building within grade
**by construction**. The taxi network (G + arms) carries the climb at its
per-letter caps; aprons grade ≤1 % to their building; junctions/G conform to the
held centerlines. Minimal-deviation throughout. `TAXI_SLACK_TERMINALS` (merged
ON) already flattens buildings at a feasible level in this shape — it just needs
the corrected per-edge caps + the per-building band.

## 9.3 The sequenced plan

**P3a — THE UNLOCK: carry the ICAO size for UNNAMED taxi edges.** Associate each
apt.dat taxi edge's `taxiway_X` letter with its **geometry** (the centerline /
route-graph edge), not its name, so `taxi_grade_cap_for_letter` returns 3 % for
the unnamed code-A arms. Fix `taxi_size_letters` (drop the `if not e.name` skip;
return a per-edge / per-geometry cap, not just a name→letter dict) and thread the
letter onto the `apt_taxi_centerlines` entries (or a parallel geometry→letter
map) so it flows into `TaxiRouteGraph.edge_cap` (already per-edge),
`network_profile`'s `narrow_lines`, and every feasibility band. **Measure this
FIRST, alone** — it is the highest-leverage fix and may lift most of the bowl at
the source (band ceiling to the terminal ~709 → ~714–718). Combine with the
banked `FIELD_ROUTE_BAND_BY_WIDTH` (P3).
*Where:* `apt_dat_reader.taxi_size_letters` (~L1418/L1434); the
`apt_taxi_centerlines` construction (`taxi_centerlines`); `taxi_routing.
build_taxi_route_graph` (consumes the per-edge cap); `network_profile` `narrow_lines`.
*Caveat:* an unnamed edge that is a genuine medial-axis *discovered* centerline
(no apt.dat row) still has no code — only apt.dat-sourced edges gain the letter.

**P4 — Per-building route-feasibility → building at closest-to-DEM.** For each
terminal pad, build the runway-anchor feasibility band along the route TO it (now
with correct caps) and seat the pad flat at `min(DEM, band_ceiling)`. Confirm
`TAXI_SLACK_TERMINALS` already does this shape and just needs the corrected band;
if it uses a coarser/global band, switch it to the per-building route band.
Buildings rise to their reachable-DEM (≈714–718); the arms reach them within 3 %
by construction. building1/building3 land at whatever their 83 m / route allows
(≈714–718) — that is the correct "as close to DEM as the route permits."

**P5 — Conform the network to the corrected held buildings + climbed corridor.**
With buildings seated high and G climbing, drive the apron/junction/G neighbours
to the consistent surface and kill the within-shape steps. This is the field-
target, lift-only within-shape conformance (the mechanism earlier drafted as
"P4a"): before the final `_project_within_bands`, re-target each soft node toward
`F` clamped to its band, **lift-only** (`elev ← max(elev, min(F, hi))`); and make
the *"CORRIDORS YIELD TO FLAT TERMINALS"* release **directional** (a corridor
yields DOWN only to a genuinely-lower served pad; otherwise it stays held). Gate
`FIELD_TARGET_CONFORMANCE`. The target must live IN the projection (its box-clamp
center), not as a weak spring (the retired soft-DEM-attraction lesson), and be
lift-biased so it only undoes the bowl, not churns the four good airports.
*Where:* `_enforce_within_shape_grade` (~L2300 target re-clamp; ~L3318/L3455 the
release). The field is `layout._network_profile_field` (`F.sample`/`F.sample_band`).

**P6 — Explicit transition only where physically forced (contingency).** If after
P3a+P4 a building's DEM still outruns even its correct 3 % route, seat it at the
band ceiling (closest-feasible, slightly below DEM) — the minimal-deviation answer
— rather than forcing a non-ICAO arm. A true ramp/retaining transition is only for
a WIDE apron that cannot grade to a climbing corridor (HECA-class). Validate
whether CYXY needs any of this once the arms are correctly 3 % (likely not).

**P7 — Retire scaffolding + lock with tests.** Once P3a+P4+P5 carry the climb,
delete the gated-off dead-ends (`APRON_FEASIBLE_LIFT` /
`_anchor_aprons_at_feasible_high`, `BUILDING_DEM_ANCHOR` /
`_anchor_buildings_at_feasible_dem` / `_relax_buildings_and_resolve`, the
standalone `O4_TAXI_SPINE` pass). Keep the keepers (`TAXI_REACH_BAND_BY_WIDTH`,
`JUNCTION_NARROW_GRADE`, `CORRIDOR_SPINE_CHAINS`, `W2_CLEAN_BANDS`, planar caps,
`TAXI_SLACK_TERMINALS`) plus the new P3a + P4 + P5 once defaulted ON. Add the two
tests in §9.5.

## 9.4 Why this is different from the things that already failed

- **Not hard anchors.** The target is a *soft* clamp inside the band; where the
  network can't reach it, POCS yields — no manufactured infeasibility (the
  `BUILDING_DEM_ANCHOR` 410 failure mode cannot recur).
- **Not the flat apron-lift.** The target is a *per-node graded* field value, not
  one flat level per apron, so adjacent aprons can't step against each other (the
  363/127 failure mode cannot recur).
- **Not the raw spine-hold.** The target is the field's *feasible* apron level
  (lift-only, band-clamped), NOT the raw spine band ceiling — a wide apron is never
  asked to grade 1 % up to a level it cannot reach (the 2882 explosion cannot
  recur). Where it truly can't reach → P6 transition, per the ruling.
- **Respects the soft-spring lesson.** The field enters as the projection's clamp
  TARGET, not a weak additive spring, so the cap projection can't override it.

## 9.5 Validation / done-criteria (pin `PYTHONHASHSEED=0` for ALL A/B)

- **CYXY:** taxiway G one smooth ≤3 % climb from the low E end (~703) up to its
  ~712–714 ceiling at the far end; the **gate arms** climb at 3 % the last
  metres to the terminal; the **terminal buildings rise to their reachable-DEM
  (≈714–718, up from the bowled ~709)**, FLAT; aprons/junctions conform (no 0.7 m
  junction steps); `test_pavement_grade[CYXY]` stays GREEN; `probe_clean` within = 0
  with the climb PRESERVED. ★ Judge against **DEM** (smoothed), never the emitted
  building levels.
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
  regression check for the P5 directional release** — confirm it still flattens.
- **P3a measured ALONE first:** with only the unnamed-edge cap fix, the terminal
  feasibility-band ceiling should rise from ~709 toward ~714–718 and the buildings
  un-bowl correspondingly, BEFORE any conformance work. If it does not, the arms
  are not apt.dat-sourced (medial-axis discovered) and P3a needs a different source.

## 9.6 Risk register (carried from the distilled traps)

- **P3a scope** — only apt.dat-sourced edges gain the letter; a genuinely
  *discovered* (medial-axis) unnamed centerline has no code. Confirm the 9 arms
  are apt.dat `taxiway_A` edges (verified 2026-06-22: they are) and that the
  letter survives into `apt_taxi_centerlines` / `TaxiRouteGraph.edge_cap`.
- **SPJC building20** — the canyon yield-DOWN case the release was built for; the
  P5 directional release must preserve it. Check first, before retiring the release.
- **HECA wide-apron canyon** — the case that may need P6; the `O4_TAXI_SPINE`
  327→2882 blow-up is the canonical warning. Watch HECA within-shape under P5.
- **`PYTHONHASHSEED=0`** for every A/B — the apron/junction partition is
  hashseed-nondeterministic (within flakes 5↔18 on the same config).
- **DEM = the SMOOTHED load.** Sample DEM via `_load_airport_dem(lat, lon)` with
  `override_dem=None` (it applies the same `apt_smoothing_pix` blur the build uses);
  do NOT pass a raw DEM. Standalone reads repo `Ortho4XP.cfg` (`apt_smoothing_pix`
  = 4 here, 8 on dev); production passes the pre-smoothed tile_dem. ★ Reading
  *emitted* (bowled) building levels instead of DEM led to a wrong "no 718 rim"
  conclusion 2026-06-22 — always check DEM, not emitted, when judging the target.
- **Plain vs augmented route graph** — the field route-band uses the PLAIN
  `shared_taxi_route_graph` deliberately (no taxiing-the-runway shortcut); keep it.
- **Identify shapes by COORDINATE, not index/ref** (indices drift across builds);
  measure A/B in a detached worktree if another session may commit concurrently.
- **`F.sample` coverage** — in deep apron interiors the field sample gap is large;
  P5 must fall back to `clamp(DEM, lo, hi)` there, not use a far centerline value.

## 9.7 Implementation map (file:line anchors)

- **P3a — the unlock:** `apt_dat_reader.taxi_size_letters` (~L1418; the
  `if not e.name: continue` skip at ~L1434 is the bug) → make it per-edge /
  per-geometry; the `apt_taxi_centerlines` construction (`taxi_centerlines`) must
  carry the letter; consumed by `taxi_routing.build_taxi_route_graph`
  (`edge_cap`, already per-edge) + `network_profile` `narrow_lines`.
- Field source: `network_profile.build_and_solve` → `layout._network_profile_field`
  (`F.sample`, `F.sample_band`, lift-only apron-plane ~network_profile.py:1454–1745).
- P3 band: `network_profile._runway_route_band` per-edge `edge_cap`
  (gate `FIELD_ROUTE_BAND_BY_WIDTH`) — built.
- P4 building placement: the `TAXI_SLACK_TERMINALS` building-flatten +
  `_runway_reach_bands` per-building route band.
- P5 target re-clamp: `_enforce_within_shape_grade`
  (unified_jacobi.py ~L2300 → before `_project_within_bands` ~L2506); bands `lo/hi`
  already computed there; held set = `held_all`. Directional release: the two
  `held_all − corridor_held_set` sites (~L3318, ~L3455).
- Seeding context: phase-1 DEM seed `_phase1_hop_priority` (~L4080); relief
  `_directional_relief` (~L4095, no reseed) — the bowl origin P5 corrects.
- Keepers to preserve: `TAXI_REACH_BAND_BY_WIDTH`, `JUNCTION_NARROW_GRADE`,
  `CORRIDOR_SPINE_CHAINS` (P2), `W2_CLEAN_BANDS`, planar caps, `TAXI_SLACK_TERMINALS`.
