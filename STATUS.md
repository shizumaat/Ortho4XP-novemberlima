# STATUS — handover (2026-06-27) — ONE-GRAPH DONE (gate GREEN); on CYXY BODY LAYER

> `docs/goal_merge_one_graph.md` is COMPLETE: `test_single_graph_acceptance.py`
> ALL 3 GREEN (spine=0, anti-gaming step, structural same-nodes), `grep geo_key
> src/` == 0, ONE `build_context` — WITH the junction-following densification ON.
> The body-layer review items below are the NEXT work, not the goal.

---
## 🔧 IN PROGRESS (2026-06-27, UNCOMMITTED) — apron→groundside connector grades ≤4%
**#3-B3 — follow the truck route, raise the apron arm, groundside meets ≤4%.**
`route_profile/anchors.py::apply_groundside_reach` (gate `O4_GROUNDSIDE_MOUTH_ANCHOR`
default ON, called LAST in `solve.py`; solve.py then re-runs `feasibility_project` on
the returned `hard` set so the apron body grades into the raised arm). The connector
mouth was coincident with the groundside but draped to apron level → `_intern` split it
into a ~5 m cliff (SVC15). USER MODEL: budget the reach over the FULL ground-truck route
(groundside edge → apron, ~55 m), keep the SVC ≤4%, and since the narrow apron arm is
welded to the SVC rect, PULL UP that arm and grade into the apron.
- BFS apron-reachability over the service network; per reachable connector the route =
  shortest `apt_service_centerline` through it, `route_len`=arc to the apron-side end,
  `base_elev`=apron there; groundside shifted (relief-preserving) into the reach band
  `[base±cap·len]` ∩ over all its connectors, clamped to DEM. Then RAISE the apron arm:
  corridor nodes (≤14 m of the centerline) take `gs_level − cap·STRAIGHT-dist-from-mouth`
  (straight → connector rect exactly ≤cap; self-taper confines the raise to the arm).
  No apron-reaching service road → stays DEM.
- CYXY: SVC15 → ≤4% (groundside 700.5 ≈ apron base +2.2 m, apron arm 697.6→699.2,
  welded both ends); 4 pieces re-levelled, ~23 route nodes pinned; SVC2 (not reachable)
  stays DEM; SVC9 baseline red untouched.
- Gate-off **byte-identical** to current-tree baseline; CYXY tests **zero net-new** (5
  standing reds gate-on==gate-off). ⚠ A/B against the CURRENT tree, NOT the stale
  `/tmp/CYXY.osm` (parallel solver WIP moved the baseline 3846→3882 nodes).

---
## ✅ SESSION CLOSE-OUT (2026-06-27) — committed on `dev`, newest first
Acceptance `test_single_graph_acceptance.py` = **3/3 GREEN** after every commit
(spine=0 verified serial `-n0`). All new behaviour is GATED default-on; gate-off =
byte-legacy. ⚠ shapeIDs are nondeterministic — identify shapes by **ref/coord**.

- `b472aed` **#3-B2** service road: connector dead-end reaches the groundside —
  extend a CONNECTOR run to the first off-pav sample so the SVC rect SHARES an edge
  with the groundside (cut, not gap). Neck SVC: apron 0.00 m + groundside 0.00 m.
- `f493fe6` **#3-B** service road: carve apron→groundside truck CONNECTOR —
  `detect_road_runs` exempts the alongside-terminal guard for a route that crosses
  WIDE aircraft pavement (touches the apron) = an SVC connector (`O4_ROAD_
  CONNECTOR_KEEP`). USER MODEL: touches apron → SVC; never touches → groundside
  (curbside = groundside, no own class).
- `5e4dd3a` **#3 split** `_merge_touching_groundside` (`O4_MERGE_GROUNDSIDE`): union
  groundside pieces sharing a ≥2 m seam → ONE surface (lot 899+1276 → 2280). The
  split root was an UPSTREAM junction-emit/overlap-clip cut of a multi-polygon
  source union — NOT neck-split or decompose (both toggled off, identical split).
- `73e9d03` **#3 cliff** SVC re-role NARROW-ONLY (`O4_SVC_REROLE_NARROW_ONLY`,
  `buffer(-7.5)` empty = <15 m) + groundside SHARES edges with SVC roads
  (`O4_GROUNDSIDE_SHARE_SVC`, drop service roles from the clearance set).
- `892ea1e` **#1** synth junction spine: don't route BETWEEN runway contacts
  (`O4_SYNTH_SPINE_NO_RUNWAY_MOUTH`) — blastpad-wrap fragmentation fixed (one
  4282 m² junction); CYXY all 9 synth spines were purely runway-mouth → dropped.
- `d19f106` **APRON DECOUPLE** `grade_graph._APRON_BODY_CHORD_MAX_M` (60 m,
  `O4_APRON_BODY_CHORD_MAX_M`): drop apron interior body↔body chords > 60 m →
  building18 apron dip 22.7%→5.4% (kept spine/building-frontage/ring-adjacent).
- (concurrent agent) `306f913` **runway-crossing** reconcile on full pavement
  extent (`O4_RW_XING_EXTENT`); `03aaf8f` runway-join validator excludes
  `runway_crossing` → spine=0.

OPEN QUEUE: **#2** SE-arm drop (upstream residue/junction-formation drop, narrowed
not fixed); **#3-C** SVC↔groundside SMOOTH grade at the now-shared edge (4%-from-
apron vs groundside-DEM) not yet verified; **#4** shape 168 → groundside (via SVC12,
too low); **#5** rough transition at shape 214 (end of A2); **#6** shapes 165+52 →
one groundside via SVC11 (too low). #4/#6 = the SVC-only→groundside pattern (the
groundside/SVC machinery is now fresh — natural next target).


Branch `dev`. Build a single airport + probe with the venv:
```
PYTHONHASHSEED=0 venv/bin/python -c "import sys;sys.path[:0]=['src','.','tests'];\
from conftest import xplane_root; from auto_patch.pipeline import build_airport_pavement;\
L=build_airport_pavement('CYXY', xplane_root(), compute_elevations=True)"
```
⚠ PIN `PYTHONHASHSEED=0` (apron/junction partition is nondeterministic → shape
indices/`shapeID` shift between builds — identify shapes by **ref/coord**, never
raw index). A build is ~60–90 s.

★ STANDING RULE (user, emphatic): use the EXISTING checks/validators/tests, NOT
ad-hoc `/tmp` scripts that re-derive logic and produce wrong numbers (a uniform
1.5 % cap in a /tmp script cost hours — the real `reach_band_sampler` uses
per-edge caps, taxiway G = 3 %). If something isn't covered, ASK to add it to the
main code or `tools/` (not `/tmp`). Real validators:
`grade_graph_validate.within_violations` (spine + body) / `.route_reach_violations`,
`building_feasibility.reach_band_sampler`, `tools/check_grade.py`,
`tools/trace_reach_route.py` (NEW: reach-route → KML), the pytest suite.

---
## ✅ DONE THIS SESSION (committed on dev, newest first)

0. **Runway-crossing reconciliation on FULL PAVEMENT EXTENT** (UNCOMMITTED; gate
   `RUNWAY_CROSSING_PHYSICAL_EXTENT` / `O4_RW_XING_EXTENT` default ON). CYXY had
   TWO 02/20 runway crossings: A (× 14R/32L, mid-body) graded smooth, B (× 14L/32R,
   ~25 m past 02/20's 20 end) had a **2.2 m / 7.7%** cross-step. ROOT: two passes
   detect crossings from DIFFERENT geometry — the junction builder
   (`pavement/runways.py _resolve_runway_crossings`) spans the runway RECTS (incl.
   displaced threshold + blast pad) so it builds B's junction, but the elevation
   reconciliation (`pavement/runway_segments.py`) detected crossings from CIFP
   **threshold-to-threshold** centerlines → 02/20's centerline ends ~25 m short of
   14L/32R → `intersects=False` → no `agreed` anchor → junction IDW-blends two
   unreconciled profiles into the step. FIX: reconciliation now DETECTS on the
   physical extent (`_runway_physical_extent`, mirrors the emit-loop extent geom)
   but still EVALUATES the agreed altitude on the CIFP threshold segment, projection
   **clamped to [0,1]** (beyond-threshold crossing → flat blast-pad elevation at the
   nearest threshold). Interior crossings project to t∈(0,1) ⇒ identical to legacy.
   RESULT: crossing B span 2.2 m→**0.00** (uniform 693.7), A unchanged 694.1;
   CYXY within-shape 42→10 (crossing 7.7% + runway/02/20 1.7% both cleared).
   ★ Suite A/B (PYTHONHASHSEED=0): gate-OFF 22 failed = HEAD baseline; gate-ON 21
   failed — **ZERO net-new, FIXED `test_runway_longitudinal_grade[CYXY]`** + 1 extra
   XPASS. Side effect: 14L/32R bends down ~1 m at B (correct — 02/20 is pinned at its
   end), nudging apron/junction BODY 302→314 (within the known CYXY body-layer WIP).

1. **ONE-GRAPH MERGE — COMPLETE** (`docs/goal_merge_one_graph.md`, the original
   goal). Spine solved DIRECTLY on the geometry nodes the validator checks
   (`grade_graph.build_unified_graph`); `route_graph.py`/`geo_key` DELETED; ONE
   `grade_graph.build_context`. `grep geo_key src/` == 0. CYXY spine 18→0.
   Acceptance `test_validator_detects_spine_step` + `test_solver_and_validator_
   same_nodes` GREEN. `test_cyxy_spine_zero` GREEN (runway-join validator fix
   03aaf8f — excludes `runway_crossing` nodes; verified 3/3 serial).
2. **route_reach validator** — `route_reach_violations`: a no-building apron whose
   feeder taxiways arrive at mutually unreachable elevations. `tests/test_route_
   reach.py`.
3. **Apron #86 / west apron** — CLOSEST-DEM-FEASIBLE model: every surface
   (buildings, aprons, spine) = DEM clamped into its per-node reach band
   [floor,ceil]. West apron filled out of its wrong-low DEM (677→693). Aprons grade
   **1 % visibility/geodesic** (apron_smooth=True), NOT DEM-draped.
4. **Synthetic junction spines** — `synthetic_junction_spine.py`: every spineless
   junction (45 of 112 at CYXY) gets a route through its taxi-network mouths
   (2→straight, 3+→star). Junction #88 (TX2 drop) 4.2 m→0.5 m; TX2 spreads its
   climb along its length.
5. **Junction-edge densification** — `lateral_spine_nodes.densify_junction_edges`
   (gate `O4_DENSIFY_JUNCTION_EDGES` default ON): subdivide every junction
   exterior edge to ~12 m (SPINE_STEP_M). Junction #97's 500 m far edge now TILTS
   695.7→698.8 following the spine (was flat 695.6).
6. `tools/trace_reach_route.py` — reusable reach-route tracer (KML + per-cap
   segment lengths), uses the real `reach_band_sampler` cost model.

★ USER MODEL (authoritative, 2026-06-26): **junctions FOLLOW their spine, graded
≤cap laterally from it; aprons grade 1 % visibility/geodesic; nothing drapes raw
DEM.** Building pads + no-building-apron feasible levels are set FIRST (closest-DEM
within the reach band), THEN the spine is smoothed between them.

---
## ⚠ WHERE WE'RE AT — apron-grading session (decouple COMMITTED d19f106)

**APRON DECOUPLE — COMMITTED (d19f106).** `grade_graph._APRON_BODY_CHORD_MAX_M`
(60 m, `O4_APRON_BODY_CHORD_MAX_M`): drop apron interior body↔body grade chords
> 60 m (keep ring-adjacent + short-local + spine + building-frontage + seam). A
wide single-polygon apron over terrain that rises >cap had a long visibility chord
pinning the building18↔16 frontage down to the route-maxed-low far interior (178 m
chord to a 695.9 node) — a "dip down then rise again". RESULT: building18 apron
worst 22.7%→5.4%, within-shape viols 384→16, dip node 698.3→700.2, spine=0,
acceptance 3/3 green.

REJECTED this session (don't repeat): (a) building-apron FLOOR/CEILING envelope in
one_solve — feasibility_project re-clamps it, no effect; (b) un-bowl by dropping
apron band ceiling — feasibility re-imposes from runway anchor; (c) enforce reach
band as HARD bounds in feasibility — the raw band UNDERESTIMATES reachability near
buildings (routes via the far centerline, not the adjacent higher building/apron),
so clamping forces the apron wrongly low (broke building22 area to 9%). KEY MODEL
FACT (user-confirmed): the reach band ALREADY includes apron crossing at the apron
cap (`reach_band_sampler` perp_climb beyond the 7.5 m taxiway corridor = 1%); so
building16's 699 IS its apron-inclusive route level. building16 is route-limited
(149 m at 1.5%), the apron 700.2 IS reachable via building18 — decouple-only is
correct. Residual 5.4% = building16 a route-limited low pad (acceptable step).

---
## NEXT QUEUE (user 2026-06-26) — ⚠ shapeIDs UNSTABLE, identify by ref/coord
1. **✅ DONE (892ea1e) Synthetic spine in impossible places** — `synthesize_
   junction_spines` anchored mouths on RUNWAYS, so a blastpad-WRAP junction got a
   star spine routing between runway contacts (runway→wrap→runway) and the slice
   FRAGMENTED it. FIX: keep taxiway/spined-junction mouths + AT MOST 1 runway mouth
   (`O4_SYNTH_SPINE_NO_RUNWAY_MOUTH` default on). CYXY all 9 synth spines were
   purely runway-mouth → dropped; blastpad junction one 4282 m² piece; spine=0
   (acceptance 3/3); body viols 431→312.
2. **SE-arm drop** — apt pavement "New Taxiway 1" north tip EAST of runway-20
   blastpad (local ~(57,673), ~1800 m²) is a HOLE (no shape) → X-Plane drapes it.
   Present with synth spine ON *and* OFF (separate from #1). The runway cuts the
   wrap into a U; the small east arm vanishes. RULED OUT: apt-ignored (apt+DSF ARE
   unioned), runway-shoulder absorption, terminal-groundside subtraction, TX-rect
   drop (TX33 was at -211,-351), apron holes, thin-orphan-slivers (>1000 m² + runway
   not in its roles), overlap-clip `_clip_keep_largest` (clip-drop debug printed
   nothing near the blastpad). REMAINING: an UPSTREAM residue/junction-formation
   drop — the east arm never becomes a junction shape (suspect small-apron-fragment
   merge assigning it to a host across the runway, or residue never claiming it).
   Next: instrument junction emit / fragment-merge for coverage at (57,673).
3. **✅ DONE (73e9d03) Groundside split + SVC cliff** — the SERVICE-ROAD CARVE +
   re-role split a parking lot: wide residue → 4% `service_junction` (~700.2), rest
   → DEM groundside (~700.9), with a 1 m clearance gap = cliff. FIX (user model: a
   service road is <15 m wide; groundside may share edges with SVC roads, cut back
   from buildings): (1) SVC re-role NARROW-ONLY — only re-role pieces with
   `buffer(-7.5).is_empty` (<15 m); wide lot residue stays groundside
   (`O4_SVC_REROLE_NARROW_ONLY`). (2) groundside SHARES edges with SVC roads — drop
   service roles from `_separate_groundside_from_airside` clearance set
   (`O4_GROUNDSIDE_SHARE_SVC`); gap 1.00→0.00 m. CYXY lot now one groundside level
   701.6-702.8, spine=0, body viols 302.
   ✅ FOLLOW-UP A (5e4dd3a) `_merge_touching_groundside` (`O4_MERGE_GROUNDSIDE`):
   union groundside pieces sharing a ≥2 m seam → ONE surface. CYXY lot @(-465,408):
   two pieces 899+1276 → one 2280 m². Split ROOT = upstream junction-emit difference
   / overlap-clip on a multi-polygon source union (NOT neck-split or decompose —
   both ruled out by toggling on/off, identical split).
   ✅ FOLLOW-UP B CORE (f493fe6) apron→groundside truck CONNECTOR. The ~6 m neck
   (apt `1206 11 10 N`, apron node 11 @(-403,408) → groundside node 10 @(-432,398))
   was NOT wide — it's narrow but `detect_road_runs` dropped its samples via
   `_alongside_terminal` (curb-road guard; building16 25 m away). USER MODEL: a 1206
   route touching the APRON is an SVC connector; one that never touches the apron
   stays groundside (curbside = groundside, no own class). FIX: a route is a
   CONNECTOR iff it crosses WIDE aircraft pavement (perp chord > 1.5×road-cap = the
   apron) → skip the terminal guard (`O4_ROAD_CONNECTOR_KEEP`); pure off-apron curb
   roads still drop (don't cross wide apron). CYXY: neck now an SVC road @(-416,404)
   connecting to the apron (gap 0.00 m); spine=0 (acceptance 3/3); gate-off legacy.
   ✅ FOLLOW-UP B2 DONE (b472aed): the SVC run stopped ~5 m short because the lot
   was subtracted from pav_union before the carve (source pavement IS continuous —
   traced apt+DSF), so the run's last on-pav sample is before the lot edge + the
   rect trim → a 5 m GAP. FIX: `detect_road_runs` tracks per-sample off-pav; a
   CONNECTOR run extends its end(s) to the first off-pav sample (the groundside
   boundary) so the SVC rect reaches it, and the overlap-clip makes them SHARE an
   edge (cut, not gap). CYXY neck: SVC connects apron 0.00 m AND groundside 0.00 m
   (was 5.15 m); spine=0 (acceptance 3/3); gate-off legacy. #3-B COMPLETE.
   ⚠ FOLLOW-UP C: SVC road grading SMOOTHLY into the groundside at the shared edge
   not yet verified (gap closed in 73e9d03, but the road 4%-from-apron vs
   groundside-DEM reconciliation at the shared edge).
4. **Shape 168 → groundside** — only airside connection is via SVC12; elevation
   seems too low. Reclassify groundside, raise to SVC12 reach.
5. **Rough transition around shape 214** at the end of A2 — smooth it.
6. **Shapes 165 + 52 → one groundside shape** — accessed only by SVC11; should be
   ONE shape, several metres HIGHER than now (SVC11 reach). (Same pattern as #4 and
   the existing "shape 154" item.)

Pattern across #3/#4/#6 (+ existing shape-154): a pavement reachable ONLY via an
SVC road = GROUNDSIDE, level = that SVC's max-grade reach; keep it connected/smooth.

---
## OUTSTANDING — earlier CYXY review batch (memory `cyxy_review_items.md`)

- **body grade** now VISIBLE at junction↔taxiway joins (body 793, was hidden by
  flat edges). The stub/A 3.2 % is RESOLVED (densify `_skip_edge` + runway-join fix
  03aaf8f). ⚠ A rare `runway_join` flicker (~2 of 10 builds) remains — same
  partition nondeterminism that shifts `shapeID`; not chased per user.
- **Crossing #82** (02/20+14L/32R runway crossing): 693.7..695.9 / 6.8 % within-
  shape — needs smoothing.
- **Shed buildings**: add DSF OBJECTs whose path starts `lib/g10/US/industrial/`
  and contains "shed" as building sources (`dsf_reader.read_dsf_buildings` only
  reads `.fac` facades today; objects need footprint resolution).
- **SVC7** (service_road, 715.2..715.6) disconnected from its parking-lot
  groundside — find the clearance/separation that drops it, keep it.
- **Dead-end road loop** at 60.7102606,-135.072653 (local ≈ -290,79; currently
  SVC10/SVC9/service_junction 708.5) → classify as apron (closest-DEM-feasible).
- **Shape "154"** = service_junction next to SVC11 → user: a shape reachable ONLY
  via an SVC road → treat as GROUNDSIDE, level = that SVC's max-grade reach.
- **SVC ramp** at 60.7131156,-135.0752334: connect apron (≈694–700) to groundside
  (≈701) as a smooth SVC ramp, not a projection off the apron.

★ DEAD ENDS that thrashed this session (don't repeat): projecting the COARSE
3-vertex spine onto edges; a hard "junction-follows-spine" grading pass (rejected);
`apron_smooth=False` closest-DEM apron body (reverted — aprons = 1 % visibility);
gating the whole junction body as is_spine (rejected — instead give each junction
a spine). The WORKING approach = densify junction edges + let the solver grade.

---
## AFTER the CYXY body layer (deferred)
Body/apron grade regressed vs the route-graph baseline (EXPECTED until the body
layer is done — user: CYXY is the sole focus). Then: re-cut compare-target
fixtures, run the full suite, other airports (HECA/SPJC/SPLP), delete ~15 legacy
elevation passes. See `docs/one_profile_solve.md` and the memory index.

## Gates / env (CYXY) — all default ON unless noted
Core: `O4_ROUTE_PROFILE_SOLVE` (next-gen solver), `O4_DENSIFY_JUNCTION_EDGES`,
`O4_SYNTH_JUNCTION_SPINE`, `O4_LATERAL_SPINE_NODES`. The `O4_RP_ROUTE_GRAPH`/
`geo_key` path is GONE.
This session: `O4_APRON_BODY_CHORD_MAX_M` (60), `O4_SYNTH_SPINE_NO_RUNWAY_MOUTH`,
`O4_SVC_REROLE_NARROW_ONLY`, `O4_GROUNDSIDE_SHARE_SVC`, `O4_MERGE_GROUNDSIDE`,
`O4_ROAD_CONNECTOR_KEEP`; concurrent: `O4_RW_XING_EXTENT`. Every one = byte-legacy
when off (verified A/B). PIN `PYTHONHASHSEED=0`.
