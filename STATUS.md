# STATUS — ROUND-4 STEP 1 DONE (`6e0f0c5`): SPJC **178 → 153** (≥1% = 12)

> THE BUDGET DIVERGENCE closed: `_bake_edge` gave apron pairs in the blend
> zone route-ARC budgets with NO building exclusion — pad-frontage chords
> earned 2-3× the flat 1%·d, so the solver graph was satisfied while the
> validator (flat, correct per the buildings-heaviest ruling) flagged them.
> Building-endpoint pairs are now NEVER baked (mirrors the blend + road-carve
> exclusions).  The movable-pad GS then enforces the chords directly:
> 178→153; ≥0.5% 54→30; ≥1% 26→12.  A/B: `final_grade_projection` adds
> nothing on top (161 vs 153) — stays gated off.  Pads flat, spine node
> 0.01 m, suite 17F/328P pre-existing.
>
> REMAINING (the step-3 tail): 123 sub-0.5% hairline (rounding class —
> test 3-decimal emit in a fresh proj_lab snapshot) + 30 real pairs
> (forensics next).  Then item B (off-source merges) → adaptive step ON →
> recut → CYXY/HECA propagation, per the approved queue.

---

# STATUS — READER UNIFICATION ROUND 1 (`dd5e6f9`): frame + splitting closed;
# BUDGET divergence remains — SPJC still 178

> Landed: (1) sidecar carries the builder's projection ANCHOR; check_grade
> uses it → validator/solver meter frames identical to float precision.
> (2) `_spine_crossing_predicate` tests ALL ctx centerlines (STRtree, cached
> on ctx) — split-agnostic (sidecar axes are split per segment-cap letter;
> membership-gated geoms diverged between readers).
>
> MEASURED: count unchanged at 178 (mix: ≥5% 4→2) — necessary, not
> sufficient.  The flagged pairs are now consistently READ but differently
> BUDGETED: with `O4_FINAL_GRADE_PROJECTION=1` the final-geometry projection
> converges (31 residual on its own graph) while the validator still flags
> ~150 pairs — pointing at ANISO ROUTE-CREDIT divergence (Allowance
> evaluation: solver bakes arc Δs∥ budgets; the validator's route wiring for
> the same pairs must differ).  NEXT PROBE (cheap, offline): extend proj_lab
> gapcheck to print solver budget vs validator allowance per flagged pair —
> the pairs are known (recurring pad-corner vertex near building30,
> b≈local(739,-16), chords 100-125 m at 1-3%).
>
> Suite 17F/328P (pre-existing).  Forensics of the current 178: top clusters
> all building30/31 pad-corner chords — ONE mechanism, budget-level.

---

# STATUS — ROUND-4 STEP-1 FINDING (2026-07-03, `9448201`): the 178 are
# READER-DIVERGENT, not unenforced

> The step-1 "building-key mismatch" hypothesis was WRONG (pads map fine).
> Proof chain: (a) the new `final_grade_projection` (final-geometry law graph,
> GS, movable pads) CONVERGES on its own graph (31 residual) yet the validator
> count stays exactly 178 — the solver law is satisfied; (b) instrumented
> `classify_pair` on BOTH readers for the same physical pairs: solver
> `crosses_spine=True→SKIP`, validator `False→ALLOW` for twin chords 1 cm
> apart — the crossing predicate flips on epsilon endpoint contact and the
> readers feed it mm-different inputs (layout meters + layout centerlines vs
> re-projected lat/lon + sidecar axes).  (c) DEAD END, measured, don't retry:
> trimming the chord ends (crosses or intersects) → 178→325.
>
> **REVISED STEP 1**: unify the reader INPUTS — sidecar carries the solver's
> exact spine geometry/frame (and possibly the per-shape skip verdicts), so
> the two readings cannot diverge; then flip `O4_FINAL_GRADE_PROJECTION=1`
> (ships gated off, ~12-15 s) to close post-solve mutations.  Steps 2-4 of the
> round-4 queue below unchanged.  Tools: scratchpad `law_diff.py` (solver
> reader, instrumented) + `law_diff_validator.py` (validator reader, no build).

---

# STATUS — ROUND-4 QUEUE (user-approved 2026-07-03): SPJC 178 → 0

> 1. **Solver-graph coverage gap** (whole ≥1% tail, ~54): 13 long
>    building-frontage chords in the validator but NOT the solver joint graph
>    — building-key detection mismatch on the solver side (V15 apron_keys
>    family, likely grade_graph.build_context).  Diagnose OFFLINE (proj_lab
>    gapcheck pair → step through classify_pair).  Fix identity, not geometry.
> 2. **Item B — off-source post-slice merges**: probe CYXY apron #120 centroid
>    (60.71179,-135.07152) through the coverage probes (one build), fix the
>    guilty pass (clip to source_pavement_union / veto), then flip
>    `O4_SPINE_STEP_STRAIGHT_M=24` ON (banked: −23 law-true, −9.5 s, SPLP
>    rests_on_source clears).
> 3. **Hairline floor (~124 <0.5%)**: (a) 51 endpoints inserted POST-solve
>    (T-weld adoptions etc.) → final micro-projection on the emitted node set
>    before to_osm; (b) 2-decimal rounding eats sub-metre budgets — test
>    3-decimals offline in proj_lab first.
> 4. **Lock + propagate**: recut SPJC compare-target; re-measure CYXY (expect
>    big free drop from movable pads); HECA by playbook (rate → audit →
>    gapcheck) LAST so only HECA-shaped classes remain.

---

# STATUS — perf round (2026-07-03) — `bb8dd16`: SPJC build **105.6 → 86.8 s**

> Profile-driven (cProfile ranked it; scratchpad profile_build.py):
> 1. **Reach band was ~half the solve** — every `band()` query full-sorted
>    ~500 centerlines twice.  `_cl_by_distance` = STRtree expanding-ring
>    iterator in exact distance order.  −11.5 s, patch identical.
> 2. **Double law build** — `_build_shape_constraints` + `build_unified_graph`
>    each ran the per-shape pair generation.  One shared ctx +
>    `shape_constraints_cached` (memo by `(id(polygon), role)`).  −7.3 s,
>    patch identical.
> 3. **Adaptive spine densify** (`O4_SPINE_STEP_STRAIGHT_M`, **default OFF**):
>    straights at 24 m / curves tight → 77.3 s, SPJC law-true 178→155,
>    verts −8.5% — but at CYXY the sparser cut lines flip a borderline
>    post-slice merge into the `rests_on_source` guard (apron #120, 27 % on
>    source; 18 m fails too, A/B-attributed).  Re-enable after the item-B
>    off-source post-slice-merge provenance fix; the knob is ready.
> Suite 17F/328P (pre-existing list), runtime 547→461 s.  Remaining perf
> levers (profiled): `_band_via` anchors loop (~11 s), `_solve_spine_profile`
> (10.9 s tottime), `_enforce_shared_vertices` (8 s ×2), projections (~15 s).

---

# STATUS — SPJC U-hole + rect-era pass retirement (2026-07-03) — `c31c15e`

> USER-reported paved-over hole FIXED: the 7,025 m² U-shaped pav_union hole
> between two parallel spines (bbox -12.0284..-12.0258 / -77.1212..-77.1196)
> was filled by `_snap_polygon_vertices_to_rect_corners` (rect-era, 5 m,
> exterior-only rebuild) — NOT by the slice (keyholes preserved it,
> face-verified).  Pass retired under the slice; defect rect now mirrors its
> twin (pavement + hole).  Law-true 178 unchanged, suite 17F identical.
>
> **Architecture ruling direction (user)**: with the slice cutting everything
> at once, rect-era geometry passes are dead weight or hazards — retire on
> measurement.  Retired so far: sliver-merge (105/105 vetoed), rect-corner
> snap (this).  Flagged, likely load-bearing: `_push_junction_vertices_off_
> taxi_rect_edges` (guards RUNWAY sloped rects, which still exist under the
> slice).  Permanent env-gated coverage probes now sit at every
> finalize/elevation geometry pass — the next coverage loss bisects in ONE
> build (`O4_COVERAGE_PROBE="lat,lon;…"`).

---

# STATUS — SPJC drive-to-zero, round 3b (2026-07-03) — law-true **1165 → 178**

> Follow-up to round 3 below (406 → 178): the "phantom anchor" thread
> resolved.  All 38 phantoms AGREE with the emitted surface (≤0.2 m) — not
> stale; the REAL oscillation source was the NON-PAD SEAT anchors
> (nobuild-apron tilt seats + contact seats) still hard in the final GS pass.
> Freeing them (they still anchor phases A/B, like pads) converges the pass
> (last_worst 1.005 → 0.019) and law-true drops 406 → **178**
> (124/28/19/3/4 by class; ≥2% = 7 total).  Gate `O4_YIELD_FREE_APRON_SEATS`.
> Suite 17F/328P/17S unchanged (same pre-existing list); pads flat ×0
> non-flat; spine node still 0.01 m.
>
> **NEXT LEVER (named, evidenced)**: the remaining ≥1.5% class (54) is ONE
> pattern — long building-frontage chords (pad-corner ↔ apron interior,
> 107–145 m, e.g. every worst pair ends at building30's ring vertex local
> (736,-19)) that gapcheck proves are MISSING from the solver's joint graph
> (13 pairs): a solver-vs-validator building-KEY detection mismatch (the V15
> `apron_keys` class of bug, now on the grade_graph side).  Close that and
> the movable-pad GS should take SPJC under ~100.  Then the sub-0.5%
> hairline (124).
>
> **Scorer note (proj_lab.py)**: unmapped airside nids must ADOPT the
> nearest solver node's candidate value — with stale patch values the scorer
> manufactures walls under large moves (three experiments mis-read WORSE
> before this fix; only small-perturbation scores were valid).

---

# STATUS — SPJC drive-to-zero, round 3 (2026-07-03) — law-true **1165 → 406**

> Suite **17F/328P/17S** — my changes add 0 (`test_no_self_overlap[SPJC]` is
> PRE-EXISTING at bc9cc61, stash-A/B verified; it was missing from the round-2
> "16F" tally).  Dev checks still need `O4_LOG_VERBOSITY=1`.
>
> ## Round-3 fixes (queue items a, b, d + hole trace)
> 1. **MOVABLE FLAT PADS (the big one, ≥5% 261→11)**: holding every building
>    seat HARD makes the final polytope INFEASIBLE through chained paths
>    (pad↔spine↔pad) even with ~0 both-hard edges — the audit only proves
>    feasibility when buildings can MOVE.  The final spine-yield projection now
>    treats each pad as a rigid flat GROUP (`feasibility_project(flat_groups=…)`,
>    ring collapses to a representative; member↔member edges vanish; broadcast
>    back after) with pads REMOVED from `yield_hard`.  Pads emit flat (verified
>    0 non-flat).  Gate `O4_YIELD_MOVABLE_PADS=0`.
> 2. **GS FINAL PROJECTION**: the vectorised Jacobi stalls (no convergence
>    guarantee); the final pass runs the scalar Gauss-Seidel POCS on the JOINT
>    edge set (shape_constraints + u_edges), 800 sweeps (`force_scalar=True`).
> 3. **SEAT COUPLING** (`build_building_seats`): pad targets projected onto
>    the pairwise polytope `|L_i−L_j| ≤ 1%·gap` (pavement-visible pairs within
>    the 200 m corridor) with reach-band boxes; fallback pads get a ring-band
>    box (immovable DEM-low seats forced the spine 5 m under its profile —
>    building26).  Gate `O4_BUILDING_SEAT_COUPLING=0`.  SPJC: 26 pads/18 pairs,
>    14 moved.  (Now partially superseded by 1 — kept: it seeds phases A/B.)
> 4. **SLIVER-MERGE SPINE VETO** (`junction_repair`): merges across
>    spine-carrying shared edges are vetoed (105/105 at SPJC — the user's spine
>    node at (-12.0334639,-77.1065028) is now 0.01 m from an emitted node, was
>    9.03 m).  Overlapping pairs are EXEMPT (duplicate coverage must merge);
>    gate `O4_SLIVER_SPINE_VETO=0`.  Post-solve subdivision was confirmed
>    already dead under `USE_PER_SURFACE_SOLVER`; the simple-shapes invariant
>    STAYS (fired 6× from non-sliver merge passes).
> 5. **HOLE PROBE (-12.03309,-77.10638) ANSWERED — no bug**: NO input covers it
>    (custom apt.dat 8.7 m away, ALL custom+Global DSF polys/objects/agp, OSM =
>    aerodrome boundary only, bezier res irrelevant).  It is a 2,521 m² island
>    ENCLOSED by the source union; the "pavement" the user sees in the sim is
>    the ORTHO PHOTO.  Fix would need a fill heuristic (contradicts V17
>    hole-preservation) or a scenery edit — user ruling required.
>
> ## Remaining 406 (all audit-unenforced, 0 fundamental) — next levers
> a. **PHANTOM HARD ANCHORS (named, evidenced)**: 38 of 162 `yield_hard`
>    members exist in NO emitted way (e.g. idx-407 @(-12.006983,-77.121437)
>    pinned 13.00 while every neighbour needs 14.2+) — they cause the ~1 m
>    POCS oscillation (last_worst≈1.005).  Blanket-freeing them measures WORSE
>    (406→506): fix at the SOURCE (why are runway-join / seam-spine anchors
>    landing on non-emitted nodes?).  Enrich `O4_DUMP_SOLVE_STATE` with hard
>    CATEGORIES to name each phantom's class.
> b. 342 of the 406 violated pairs ARE in the solver graph (left over by the
>    oscillation, → fixed by a); 13 missing long apron chords (100-190 m,
>    building-frontage class) + 51 endpoints unmapped (created POST-solve:
>    T-weld inserts etc.) are the true coverage gap — small, do after a.
> c. Perf: build 92 s → ~105 s (GS pass + envelope Dijkstras) — active-set
>    sweeps would reclaim most; also the standing 2× shape_constraints build.
>
> ## Fast iteration harness (NEW — use this, not full rebuilds)
> `O4_DUMP_SOLVE_STATE=/tmp/spjc_solve_state.pkl` (solve.py) dumps the
> final-projection inputs; scratchpad `proj_lab.py` re-runs projection variants
> OFFLINE (~3 s vs 117 s) and scores them with the TRUE law
> (`check_grade._check_within_shape` on the emitted patch geometry + sidecar,
> patch nids → solver idx by KD-tree in the patch meter frame, 95.3% airside
> coverage).  Lab reproduces production exactly (406 = 406).  Modes:
> baseline / diagnose / gapcheck / nophantom / freehards / who "lat,lon".

---

# STATUS — SPJC drive-to-zero, round 2 (2026-07-03) — HEAD `9399d9c`

> Suite **16F/329P/17S** (−1 vs baseline).  Dev checks: `O4_LOG_VERBOSITY=1`.
>
> ## Round-2 fixes (user: holes + skeleton fidelity)
> 1. **HOLE KEYHOLES** (`global_slice`): TWO spur cuts per interior ring
>    (nearest spine, else boundary; second from the antipodal ring point) —
>    ONE cut makes a SLIT polygon whose doubled edge collapses under vertex
>    dedup and paves the hole over.  SPJC: 19 of 36 holes paved-over → **1**
>    (28 open ✓, 7 under building pads ✓).
> 2. **SIMPLE-SHAPES INVARIANT** (pipeline, pre-solve): airside shapes with
>    interior rings (merge passes can rebuild an annulus) are decomposed via
>    the rect-era `_decompose_polygon_with_holes` (its old home junction_emit
>    is bypassed under the slice).
> 3. **`_resample` → `shapely.segmentize`**: even respacing MOVED original
>    spine vertices (bends/arcs); densify now preserves every input vertex.
> 4. **DIAGNOSED, next round**: the user's missing spine node
>    (-12.0334639,-77.1065028) IS a face vertex at raw slice output (0.01 m)
>    and is destroyed downstream — the rect-era SLIVER-JUNCTION MERGE unions
>    adjacent faces and dissolves spine-carrying shared edges.  Fix: exempt
>    merges across spine edges, or retire the sliver merge under the slice
>    (conformant faces don't produce the decomposition slivers it targets).
> 5. SPJC law-true 539 → 1165 = the SAME classes (per-pad seat conflicts >3 %
>    + projection hairline) over newly-SURVIVING stand pavement — all funnels
>    into the seat-coupling work (round-1 item a).
>
> ## SPJC queue (updated)
> a. Building seat coupling (biggest, unchanged).
> b. Sliver-merge vs spine edges (item 4 above — restores skeleton fidelity).
> c. Source-level pavement hole probe (-12.03309,-77.10638).
> d. Projection hairline once (a) lands.

---

# STATUS — SPJC drive-to-zero, round 1 (2026-07-03) — HEAD `783d349`

> Suite 17F/329P/16S (stable baseline).  `O4_ROUTE_ARC_SPINE` default ON.
> **Reminder: dev grade checks need `O4_LOG_VERBOSITY=1`** (sidecar gate).
>
> ## Findings + fixes (user JOSM round 2, SPJC)
> 1. **Building↔spine 1 % now ENFORCED** — the reported 3.5 % chord
>    (building-10031 ↔ spine, 86 m) was *legalised* by two relaxations: the
>    4 % road-frontage carve (service road hugs the terminal) and the
>    apron↔taxi blend (which since v14.1 also blends against 4 % service
>    spines).  Both now exclude building-endpoint pairs (`grade_law`), and
>    service spines never blend aprons (`grade_graph`).  The pair solves to
>    exactly **1.00 %**.
> 2. **SPJC law-true 174 → 961 — the law got honest, the surface didn't get
>    worse.**  Audit: 0 fundamental / 961 unenforced, POCS→0 in 77 sweeps.
>    The >3 % class (~380) = pre-existing PER-PAD SEAT CONFLICTS
>    (neighbouring pads seated up to 2.6 m apart — building26 class) that the
>    blend had waived; the 1.06 %-ramp class = projection residual around
>    raised aprons.  **NEXT BIG ITEM: seat COUPLING in building_feasibility —
>    choose jointly-feasible pad levels (audit proves they exist), then the
>    yield projection converges.**
> 3. **Slice coverage is SOUND** — faces ≡ slice input exactly (verified
>    standalone AND in-pipeline via new `debug_pts` tracing).  The
>    user-visible holes are SOURCE-level: `pav_union` never had that
>    pavement.  One cause fixed: the DSF overlay gate dropped WHOLE polygons
>    ≥80 % inside apt.dat — their outside strips (real pavement) are now
>    kept (≥50 m², SPJC +5 polys).  At least one reported hole remains
>    unexplained at source level (probe -12.03309,-77.10638; rect model
>    identical) — trace which apt.dat/DSF/OSM input should cover it.
> 4. **"Dropped through-line" at -12.0332845,-77.106591 is NOT a bug** — the
>    apt.dat route network genuinely ends at that stand (tool + production
>    agree); v13 route-verbatim = no route, no spine.  The area LOOKS broken
>    because of the source-level pavement hole next to it (see 3).
> 5. **Apron-scope architecture ANSWERED (user question)**: no geometry
>    refinement needed — grading scope comes from BUILDING PROXIMITY via the
>    law: building-endpoint pairs are 1 % (never blended/relaxed), the rest
>    of a mixed face grades at taxi law with spine credit.  shapeID-70-style
>    mixed faces are fine under this model once seats are coupled.
> 6. Debug infra: `O4_COVERAGE_PROBE="lat,lon;…"` prints probe owners after
>    each post-slice pass; `build_global_slice_faces(debug_pts=…)`;
>    `O4_SLICE_SOURCE_CLIP=0`.
>
> ## Perf (user report: build time ~doubled) — FIXED at `7c6d33c`
> Measured SPJC: rect 51.6 s vs global slice **160 s** (solve 25.7→140 s,
> 5.4×).  Root: ~500 UNCHAINED route pieces made the solver's nearest-line
> scans quadratic (25 M `_project` calls / ~90 s).  Fixes: STRtree caches on
> GradeContext for nearest-route/centerline/spine-membership; vectorised
> Jacobi `feasibility_project` under the slice (was gated off).  Now
> **92 s** total (solve 75 s) — and the better projector also dropped SPJC
> law-true 961 → **681**.  Suite runtime 8 min → 4.5 min.  Remaining solve
> budget if needed: shape_constraints is built twice per shape
> (build_unified_graph + _build_shape_constraints), node_bands ~36 s.
>
> ## SPJC to zero — remaining queue
> a. Building seat coupling (the 961 → small; biggest lever).
> b. Source-level pavement holes (item 3 probe).
> c. Hairline projection residual (~1.06 % ramps) — tighten the yield
>    projection once seats stop conflicting.
> d. Then the localized 0.2-0.45 m solver dips (STATUS v15 item C).

---

# STATUS — handover (2026-07-02, session 2) — **V15: JOSM-review fixes round 1 done (waviness/buildings/welds/bridges/groundside)**

> HEAD `d852f5a`+docs, tree clean, `O4_ROUTE_ARC_SPINE` default ON.
> Suite: **17 failed / 329 passed / 16 skipped** — identical list to the v14.1
> baseline (`/tmp/suite_failures_20260702_v14_1.txt`); the v15 fixes added 0.
>
> ## V15 (user JOSM/in-sim review round)
> 1. **Waviness**: elevations were 0.1 m-quantized at writeback + emit → 1-4 %
>    grade stairs every ~5 m.  Now 2 decimals end-to-end; worst-ring kink
>    counts −60 %.  Remaining: localized 0.2-0.45 m solver dips (below).
> 2. **Building 1 % rule**: frontage-seat keys were ROLE_APRON-only — under the
>    slice buildings front ROLE_JUNCTION corridor faces, so seats fell back to
>    the legacy whole-ring median.  Fixed → **CYXY 174 → 114 (< 138 rect
>    baseline)**.
> 3. **Unwelded T-vertices** (user's 60.7220178,-135.0806001): final
>    insert-only weld (tol 0.01, overlays included, overlay receivers ADOPT
>    donor altitude) → CYXY 7 → 0.  SPJC has 1 left (apron node 0.03 m off a
>    building edge — beyond the tight weld tol, kept to avoid hairline-overlap
>    regressions).
> 4. **Boundary bridges**: keep-largest after buffer(0)/subtraction discarded
>    the CYXY north wedge (terrain hole over faulty DEM).  Now every part
>    ≥100 m² emits (overlap-guarded, last-word re-clip, boundary-proximate
>    vertices take the ribbon clamp) → bridge area 210k → 252k m², north wedge
>    back.  Rect baseline 333k — the delta is inner-edge DEPTH (100 m
>    perpendicular vs the rect-era pavement-walk); tune if the sim still shows
>    a gap >100 m from the boundary.
> 5. **Groundside via service roads**: svc-only faces are service_junction at
>    ANY width → the runway touch-chain severs at roads and lots demote via
>    the existing reclassifier → CYXY groundside 31.8k → 73.6k m² (baseline
>    76.5k); road-only lots 2 (was 1).
>
> ## Outstanding (categorized)
> **A. Grade (law-true)** — CYXY 114 ✓(<138), SPJC 174 ✓(<198), SPLP 0 ✓,
> **HECA 5061 vs 4138 ✗ undissected** (+4 cross-shape desyncs, runway
> longitudinal red; suspect building seats at scale — run the session-1
> playbook: rate → audit → forensics).  SPJC's worst = pre-existing
> building26 2.6 m relief (10 pairs).
> **B. Geometry** — `rests_on_source` red ×3 (SPLP #19/#20 82k/34k m² at
> 20-24 % on source, CYXY #141 3.9k m²): NOT slice faces — the slice input is
> now source-clipped, so these are created/merged by a POST-slice pass
> (provenance tracing next; likely lot/fragment merges or reclassifies).
> 1 residual T-junction at CYXY (pre-existing class).
> **C. Visual** — localized solver dips (SPJC apron -10036 one 0.45 m jog,
> -10054 0.2-0.35 m dips at ~(395-435) ring arc) = envelope clamps in the
> body solve; bridge inner-edge depth (C above).
> **D. Test debt** — compare-target recuts (SPJC, SPLP ×2) once v15 geometry
> settles; test_pavement_grade universal-zero reds (by design); 2 stale
> apt_dat_reader tuple tests; dsf cluster-bridge; CYXY spine-zero /
> route-reach acceptance thresholds are rect-era — CYXY now beats baseline,
> so re-baseline them.
>
> **Sidecar is now DEBUG-gated** (`6c65a30`): `<patch>.axes.json` is written
> only when `config.LOG_VERBOSITY > 0` (env `O4_LOG_VERBOSITY=1`) — set it for
> any dev build whose patch you want to check law-true with the CLI; production
> patch dirs stay clean.  Progress window: content-fit ≤6 rows / scroll >6 /
> auto-close on all-done (failures keep it open).
> Debug helpers added: `O4_BRIDGE_DEBUG=1` (per-run bridge emit trace);
> scratchpad tools worth recreating: tvertex_scan.py, edge_profile.py
> (ring-roughness), vio_forensics.py, bridge_dump.py.

---

# STATUS — handover (2026-07-02, session 2) — **V14.1: route-arc GLOBAL SLICE default ON; SPJC+SPLP at/below baseline, CYXY close, HECA open**

> Everything committed on `dev` (HEAD `fa69b21`), tree clean.
> **`O4_ROUTE_ARC_SPINE` DEFAULT ON** (user 2026-07-02, for JOSM / X-Plane review;
> `O4_ROUTE_ARC_SPINE=0` restores the legacy rect pipeline).
> Suite at v14.1: **17 failed / 329 passed / 16 skipped**
> (list: `/tmp/suite_failures_20260702_v14_1.txt`).  Rect-residue junction
> invariants SKIP under the gate (they describe rect-residue geometry);
> `test_pavement_rests_on_source` deliberately NOT skipped (genuine guard,
> red ×3 — see open items).  SPLP compare-targets red = geometry
> legitimately shifted, recut when v14 settles.
> ⚠ Ortho4XP caches `auto_patch.*` — restart Ortho4XP after any commit.

## v14.1 fixes (this session, after the default flip)

1. **SPLP seam cliff (user-reported regression) — FIXED, 24 → 0.**
   `nudge_runway_corners_at_seam_junctions` assumed a seam piece is a SMALL
   terrain-pinned stub; a sliced face reaches the tile line from 480 m away, so
   it dragged runway 02/20's threshold 5.4 m off its FAA profile.  Skipped under
   the global slice (`pipeline.py` call site) — seam pins stay truth-hard and the
   solver spreads the drop (cap × 480 m ≫ 5.4 m).
2. **Service roads = road-cap spines (user ruling) — CYXY 300 → 174.**
   Service centerlines are sliced; NARROW faces riding only a truck route
   (width ≤ 25 m) emit `ROLE_SERVICE_JUNCTION` (restores `road_zone`); wide
   pavement crossed by a truck route stays apron.  `grade_graph.build_context`
   adds service lines as SPINES at `SERVICE_ROAD_MAX_GRADE` under the slice
   (longitudinal 4 % solve along the road); `taxi_axes_ll` exports service axes
   at the road cap (was accidentally 1.5 %).
3. **Rect-era test triage**: `test_junction_invariants` + `test_junction_rules`
   skip under `ROUTE_ARC_SPINE` (rect-residue semantics, kept for legacy path).

## What happened this session

1. **Audit of the 1242→2533 doubling** (the old rect-path A/B): NOT worse grading —
   the violation *rate* went DOWN (5.64%→5.11%); the count doubled because the arcs +
   their corner legs were both sliced per-junction → 2.25× constrained pairs
   (sliver faces, dense cut nodes). The metric itself was also off: the CLI
   `check_grade` ran context-free (no axes/routes → no spine/blend/aniso credit).
2. **USER RULING mid-session: with the full spine, disable taxi-RECT creation — the
   spine runs everywhere.** Implemented: `O4_ROUTE_ARC_SPINE=1` now implies the
   curve-native **global slice** (`apply_route_arc_spine` runs at the slice stage;
   pav_union cut once by route+arc ways; rect emit / junction_emit / fillet /
   synthetic-spine / junction_spine all bypassed). `2f828e1`.
3. **Axes sidecar** (`10eb088`): `layout.to_osm` writes `<patch>.axes.json`;
   `tools/check_grade.py` auto-loads it → the standalone CLI now applies the SAME
   within-shape law as the solver/suite. Context-free numbers (1242 etc.) are
   obsolete; compare law-true only.
4. **Solver adaptations** (found via `tools/grade_feasibility_audit.py` — all
   violations were 0-fundamental/all-unenforced, POCS→0):
   * **No dedup for route-arc slice input** — the 3.5 m paint-dedup ate short
     junction connector fragments (481→399), disconnecting spine chains: PHASE A
     froze adjacent route chains up to 2.6 m apart (frozen-spine walls).
   * **`classify_faces` v2** — corridor by geometry (width = area/shared-edge), not
     centerline count; big multi-CL faces are JUNCTION when ≥55% of area is within
     25 m of their centerlines; only true stand/terminal pavement keeps 1 % apron law.
   * **SPINE-YIELD projection** (`route_profile/solve.py`, global-slice only, LAST
     before writeback): most nodes are spine under the slice, so "both-hard =
     genuine step" is wrong; re-project with only truth anchors hard (runway/CIFP,
     tile-seam pins, building seats, groundside pins).

## Scoreboard (law-true `tools/check_grade.py <patch>` with sidecar, within-shape)

| fixture | rect baseline (gate OFF) | v14.0 | **v14.1 (HEAD)** |
|---|---|---|---|
| SPJC | 198 | 185 | **175 ✓ below** |
| CYXY | 138 | 300 | **174** (1.26× — building seats remain) |
| SPLP | 0 | 24 | **0 ✓ = baseline** |
| HECA | 4138 | 5275 | 5184 ✗ (+4 cross-shape desyncs) |

Open items (named, diagnosed):

- **CYXY 174 vs 138**: building-frontage seat conflicts — pads seated at
  incompatible levels 1–2 m apart (production pins seats; the audit proves a
  compliant field exists if seats could move → the building-FEASIBILITY seat
  solver must pick frontage-compatible levels, or the spine-yield should treat
  each building as a movable FLAT group like the audit does). Worst spots:
  (88,-399), (117,-533) + building-10002, (-243,914).
- **HECA 5184 vs 4138**: not yet dissected (builds clean end-to-end; suspect the
  same building-seat class at scale + 4 cross-shape desyncs).
- **`test_pavement_rests_on_source` red ×3 (CYXY/SPJC/SPLP)** — GENUINE: the
  slice emits every face of the local `pav_union`, which contains area that is
  NOT apt.dat/DSF source (SPLP faces #19/#20: 82k/34k m² at 20-24 % on source —
  pavement over grass in the sim). The rect pipeline separated/dropped that
  area (groundside separation, residue rules). Fix direction: intersect the
  slice input with `source_pavement_union`, or run the groundside/clearance
  separation before the slice. **This is the top JOSM-visible defect.**
- `node_altitudes` are written at 0.1 m resolution — at sub-metre pair distances
  rounding alone can eat the budget; part of the <0.5 %-over tail is noise.

## Where things are

- Wiring: `pipeline.py` (`_global_slice_spine = CURVE_NATIVE_SPINE or
  ROUTE_ARC_SPINE`, slice branch ~line 3360; the old pre-slice hook is gone).
- Gate: `config.ROUTE_ARC_SPINE` (env `O4_ROUTE_ARC_SPINE`, default OFF).
- Solver: `route_profile/solve.py` — `truth_hard` captured pre-freeze; SPINE-YIELD
  block right before `_writeback`.
- Faces: `pavement/global_slice.py::classify_faces` (route-territory rule).
- Sidecar: `layout._write_axes_sidecar` + `tools/check_grade.py::main`.
- Iteration tools (session scratchpad patterns worth recreating): full-build script
  (`build_airport_pavement` → `to_osm` → CLI check); law-true probe = build →
  `verification.taxi_axes_ll/taxi_routes_ll` → `check_grade._check_within_shape`;
  `tools/grade_feasibility_audit.py <ICAO>` classifies fundamental vs unenforced
  (env gates apply — run with `O4_ROUTE_ARC_SPINE=1`).
- Debug: `O4_STEP_DEBUG=1` prints one_solve residuals by node type ("seam" there
  = any base_hard node incl. frozen spine, NOT just tile seams).

## NEXT SESSION

1. **`rests_on_source` fix** (top JOSM-visible defect): stop emitting faces over
   non-source pavement — intersect the slice input with
   `source_pavement_union` (+ runway), or run groundside/clearance separation
   before the slice. Then re-check the invariant ×4.
2. **CYXY building-seat frontage coupling** (174 → ≤138): frontage-compatible
   seat levels in `building_feasibility`, or movable-flat-group buildings in the
   spine-yield projection.
3. **HECA dissection** (rate + audit + forensics — the session-1 playbook) +
   its 4 cross-shape desyncs.
4. Recut SPLP/SPJC compare-target fixtures once v14 geometry settles; re-baseline
   `test_pavement_grade` counts.

Suggested kickoff:
> "Continue V14.1 (STATUS.md + memory pav_skeleton_medial_axis_spine.md):
> route-arc global slice default ON; SPJC 175<198 ✓, SPLP 0 ✓, CYXY 174 vs 138,
> HECA 5184 vs 4138. Fix rests_on_source (slice emits pav_union area that isn't
> apt.dat/DSF source — pavement over grass), then CYXY building seats, then HECA."

## Pre-existing suite reds (unchanged)
19 at `dev@2f828e1` — identical list to `/tmp/suite_failures_20260702.txt`.
