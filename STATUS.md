# ══════════════════════════════════════════════════════════════════
# HANDOVER QUEUE (part 34 END, committed milestone) — START HERE
# ══════════════════════════════════════════════════════════════════
# The day's arc: naive weld exploded CYXY to 1.55M airport triangles →
# chain identity + Noah's three design rulings (gap-fill spine ·
# pavement-node rule · groundside standoff) landed the FULL grading
# system at 15,726 triangles (pre-weld no-grading baseline: 26,727).
# Installed tile = round9.  Method: tools/chain_divergence_audit.py
# (gate = ZERO near-parallel pairs; ONE lens = 10⁵-10⁶ triangles) →
# ~8 min build → ~3 min warm bake (tools/run_tile_build.py 60 -136 1
# "<Custom Scenery zOrtho4XP_+60-136>") → tools/mesh_hotspot_cells.py.
# Everything below is detailed in the session sections that follow;
# docs/chain_identity_one_solve_plan.md is the architecture doc.
#
# THE QUEUE (ordered):
# 1. to_osm CHORD-REMOVER HUNT: ONE 1,057 m junction edge (junction
#    #101, nids -1692→-1785) survives all three densify passes — the
#    layout ends clean, so the remover is INSIDE to_osm.  Suspects:
#    the chain-consistent needle removal (no chord cap) or a nid-drop
#    path.  Instrument to_osm removals on that way; cap; re-verify
#    the 60 m rule end-to-end.
# 2. SEAM-DIP TRACE (in-sim round 4): intra-band run seams sit ~25 cm
#    low (vertex -5167-class, a 0.72 m jog pair) — suspect the
#    run-end taper borrowing the run-end reference instead of the
#    local edge read.  Emitter trace vs corridor expectation, then
#    fix.  Noah's invariant: shadow rows mirror the pavement line
#    EXACTLY.
# 3. HANGAR RESIDUAL: 1.40 m coincident pair near
#    60.7082163,-135.0715405 — attribute (may be the lawful designed
#    apron↔groundside step; if strip-related, extend the standoff).
# 4. HAIRLINE ENCLOSURE LEAKS: census hole 27 (60.7133,-135.0646) is
#    enclosed in final geometry but OPEN at gap-emit time (sub-mm
#    pavement seam gap) — pre-solve pavement conformance closes it
#    (the slice B arc); the gap then fills with no further change.
# 5. BUILDING-PAD GAP PARENTS: 8 census holes contain building pads —
#    add pads to the gap-bounding union (flat value authorities,
#    apron-family envelope).  Designed in the plan doc, not built.
# 6. SLICE B PROPER: bands/skirts/gap spines into the ONE solver
#    graph + pre-solve construction (docs/chain_identity_one_solve_
#    plan.md §Slice B — includes the perf levers and Noah's endorsed
#    gap-fill/drainage-spine design as centerpiece).
# 7. LEGACY CHAIN DELETION: blocked ONLY on open-frontage corridor-
#    band quality now (gap supersession already retires wholly-inside
#    strips; measured legacy-off: tears 7 / crossings 40 / ways 759).
# 8. HOUSEKEEPING: FULL SUITE AT THE MILESTONE COMMIT = 13 failed /
#    683 passed (improved from 14/672 pre-milestone):
#    test_solver_and_validator_same_nodes now GREEN (healed by the
#    node-rule work) · dsf flag-gating fixed by the other session's
#    cc903ed · NEW red: test_runway_longitudinal_grade[SPLP] — the
#    known at-cap marginal class (1.5x %), likely surfaced by the
#    densified runway-edge stations; triage with the SPLP profile
#    checker · remaining 12 = the documented pre-existing set +
#    compare-target ×3 drift (EXPECTED until Noah approves output
#    and fixtures recut) · the 4 untracked DSF tools belong to the
#    DSF-object arc — commit with that arc, not this one.
#
# RULINGS LEDGER (all Noah, 2026-07-09, all implemented unless noted):
# weld (strips share pavement chains) · skirt anchored at RUNWAY END
# (dev 9345739) · one-solve doctrine + solver perf first-class + no
# cm precision (grading UNDER pavement) · zone-3 vertical face only
# at TRUE outer edges (smooth blend between parallel pavements: NOT
# yet built — slice B) · fixture recut only after final approval ·
# pavement value always wins at pavement nodes · gap-fill + drainage
# spine design · grading shapes NEVER create nodes on pavement edges
# · no strips around groundside (+ buildings) · pavement edges keep
# nodes every ~60 m so the solver holds the edge.

# STATUS — SESSION 20260709 (part 34): CHAIN IDENTITY SLICE A —
# VERDICT: THE ADJACENT-GROUND PROJECT FLIES.  CYXY tile bake with the
# FULL WELD + BANDS: 24,333 airport-region triangles, BELOW the
# pre-weld no-bands baseline (26,727; the naive weld exploded to
# 1,552,854).  Total tile 633,530 vs 635,934 — welded bands are
# triangle-NEGATIVE.  Doc: docs/chain_identity_one_solve_plan.md.

## USER RULINGS (Noah, part 34)
1. ONE-SOLVE DOCTRINE (architecture): all rules/laws live in
   grade_law; the solver solves as many elevations as possible in ONE
   pass; minimize/eliminate post-solve geometry or elevation mutation.
   Approved slices: A chain identity (DONE, this session) → B solver
   absorption (bands/skirts as solver nodes; a shared vertex = ONE
   variable — the weld/consensus/conformance apparatus evaporates) →
   C emit reduction.
2. SOLVER PERFORMANCE is a first-class goal, architecture-level and
   opportunistic.  Precision reframe: NO cm/mm fidelity needed — the
   output is grading UNDER pavement, layered over; smooth required,
   simplification fine.
3. Zone-3 vertical faces lawful ONLY at a TRUE outer edge (no shapes
   beyond); between parallel pavements the ground must smoothly blend
   runway-drainage → taxiway-shoulder (NOT built yet — slice B work).
4. Fixture recut ONLY after Noah approves final in-sim output.
5. Corner arbitration: PAVEMENT value always wins at pavement nodes.
6. Skirt anchor fix committed separately: dev 9345739.

## THE METHOD (this is the durable lesson)
* GATE = ZERO near-parallel constrained pairs.  ONE lens (mm-scale,
  metres long) Ruppert-refines to 10⁵-10⁶ tile triangles — "only a
  few sites left" is not a state, it is an explosion.  Slice A
  round 1 REDUCED sites 136→36 and the bake got WORSE (3.0M).
* The loop: tools/chain_divergence_audit.py (patch-level T-vertices
  by perp bin + near-parallel pairs + coincident nids, by role pair;
  seconds) → tools/full_airport_build.py (~8 min) → warm tile bake
  ~3 min (tools/run_tile_build.py 60 -136 1 "<Custom Scenery
  zOrtho4XP_+60-136>" — the runner now takes build_dir as arg 4) →
  tools/mesh_hotspot_cells.py (25 m cells; hotspots map 1:1 to
  audited lens sites; µm-scale medians = ping-pong, m² = lawful).
* Pipeline now prints a post-weld residual-divergence report (any
  conformance violation surviving the final weld, with lat/lon).

## THE FIX CHAIN (landing order; all in the working tree, UNCOMMITTED)
1. WELD-ROW DIET (adjacent_ground): band inner rows at d0=0 are the
   PAVEMENT CHAIN SUBSEQUENCE (ring vertices — every k==0 station IS
   a ring vertex — plus run endpoints); interior 5 m stations left
   the weld row.  Final-weld insertions 3,527 → ~540; patch nodes
   12,895 → 10,032.
2. CONFORMANCE SILENT-BAIL BUG (conformance.py): a candidate landing
   on TWO edges of one ring was inserted twice → invalid rebuild →
   the shape silently kept ZERO welds (the immortal
   junction~runway_clearance 53).  Fix: first-edge-wins ownset
   dedup + loud bail WARN.
3. Boundary-frozen `_merge_coincident` (clearance, Opus agent):
   frozen_predicate at 1e-6 on the static boundary; frozen coords win
   verbatim, never the mean.
4. CONFORM-ADOPT (adjacent_ground): band ring edges SPLIT at static
   vertices within 0.2 m, then all vertices SNAP onto the static
   exterior — a soft ring adopts the static chain wherever it runs
   within tolerance (kills the mid-span-edge-next-to-corner class).
5. TWO-PHASE SEAM-SAFE legacy _finalize (clearance, Opus agent):
   collect all piece rings first → mm-key shared-seam set → decimate/
   drop-sharp/merge protect seam vertices.  (Per-piece independent
   decimation had desynced sibling seams — ONE such lens at
   60.7014,-135.0630 was 1.57M triangles.)
6. ★ THE KEYSTONE — NID-LEVEL FINAL WELD in layout.to_osm: the 0.5 m
   canonical interning MOVES vertices AFTER the layout-level weld
   (the KPHX 9.3°→0.36° note admits it), so the T-vertex weld re-runs
   on the FINAL nid rings at the FINAL coordinates (insert existing
   nids into ways whose edges they lie on; consensus rides the nid —
   no altitude bookkeeping).  15 insertions killed every sub-cm
   divergence class.  Must stay the LAST geometry-affecting step.
7. SELF-LENS REPAIR (adjacent_ground._repair_self_lenses): the snap
   can collapse a thin cut residue into an out-and-back sub-µm pinch
   INSIDE one ring (band -10391 doubling back along the runway line
   = the last 182k-triangle hotspot).  Insert the ring's OWN vertices
   into edges they graze (≤5 mm) → exact self-touch → buffer(0)
   splits into clean lobes.
* MEASURED NEGATIVE (kept in code comments): projection-onto-chord
  needle repair is WRONG — near-parallel 136→200; most tips sit on a
  welded HOST edge whose ring does not reference the nid, and moving
  the tip pulls the chain off the host.  Removal + chain-consistent
  partner removal stays.

## VERIFIED (part 34)
* chainid6 audit: near-parallel 136→1 (a legacy surface_clearance
  pair; the healthy pre-weld baseline itself reads 1) · T-vertices
  136→8 (all ≥1 cm legacy/wall classes).
* CYXY bake (chainid6 patch installed at
  Patches/+60-140/+60-136/CYXY_auto.patch.osm, tile BAKED and READY
  TO FLY): airport bbox 24,333 tris (baseline 26,727, naive weld
  1.55M, slice-A round 1 3.0M); worst hotspot cell 113 tris at
  m²-scale medians.  Pre-weld patch + mesh preserved in the session
  scratchpad (bake_ab/).
* FULL suite: 14 failed / 672 passed — all mapped: the documented
  pre-existing set (pavement_grade SPLP/SPJC/CYXY/HECA,
  no_self_overlap[SPJC], route_band_zero[SPJC], cyxy_route_reach,
  edge_budgets, dsf_object flag-gating, cyxy_spine_zero_no_bowl
  22 cm) + compare-target ×3 weld drift (EXPECTED per ruling 4 —
  recut awaits approval).  ⚠ test_solver_and_validator_same_nodes
  attribution NOT A/B'd vs the pre-slice-A tree (likely the same
  CYXY solver-arc family).
* 68/68 skirt tests + 18/18 adjacent-ground emitter tests green.

## IN-SIM REVIEW ROUND 2 (Noah flew the slice-A bake) + fixes
Cliffs along runways CONFIRMED FIXED in-sim.  Three findings:
1. DENSITY (shape 261, "a thousand nodes along the runway edge") =
   the LEGACY surface_clearance chain (5 m stations + every neighbour
   vertex the final weld inserts).  ANSWER: bands got the diet, the
   legacy chain did not — it is the slice-5 deletion candidate, now
   the largest remaining node diet.  NOAH'S DESIGN (endorsed, slice B
   centerpiece — in the plan doc): for ENCLOSED gaps between
   pavements, ONE shape whose boundary = the pavement chains verbatim
   + ONE interior drainage SPINE (a swale with FAA min/max transverse
   grades as solver constraints); a few dozen solver variables per
   gap, chain identity structurally free, ruling-3 smooth blend by
   construction.  Hybrid for gaps wider than the facing corridor
   reaches; spine becomes a small tree for L/T/Y gaps (medial-axis
   machinery exists).
2. SPIKE TRIANGLES (447/448/449, 150-220 m): three mechanisms found
   and fixed: index-gap run bridging → physical-distance bridge;
   corner-fan outer-point chords across a skipped end sweep →
   OUTER-JUMP FLUSH (>4 stations closes the ring) + NO FAN when
   either flanking edge fails the station-reference test (the skirt
   owns the end zone); crescent clip residues → buffer(-0.75) width
   gate.  TWO of three shapes dead.  REMAINING (1): a 156 m × 7 m
   "isolated deep ray" — two stations lawfully marching to a real
   terrain violation near the reach limit that no neighbour
   corroborates.  NEXT FIX (lockstep required): neighbour-support
   clamp on the daylight march, defined once in grade_law/config and
   mirrored in the corridor validator — an unmirrored emitter clamp
   would mint validator findings.
3. UNMERGED-NODE CLIFF (junction 111 @ 60.6971601,-135.0592654,
   Δ1.71 m): a band vertex fused with the junction corner only inside
   the 0.5 m emit-interning bucket, where the >1.0 m altitude split
   minted a wall twin.  FIXED at _intern (pavement-wins, ruling 5): a
   graded_strip NEVER emits a wall twin against an authority-claimed
   node — it adopts the authority nid (soft claim joins the plain
   mean only).  Deliberate walls (retaining_wall, skirt lifts) keep
   the twin path.  VERIFIED: one node, 706.11.
Audit after all fixes (chainid10): T-vertices 6, near-parallel 1
(= the healthy baseline's own count); emitter tests 18/18.

## ROUND 3 (Noah: "confirmed, proceed") — the three big pieces
1. DAYLIGHT SLOPE-LIMIT LAW — DONE, lockstep (Opus-implemented):
   config.ADJACENT_GROUND_DAYLIGHT_SLOPE_LIMIT (2.0) +
   grade_law.adjacent_ground_supported_depths (distance-weighted
   two-sweep limiter; fan stations share a coordinate → zero
   allowance → fan rays fully suppressed) + both builders' outer
   scans + check_adjacent_ground mirror (reproduces the raw scan,
   applies the law, exempts columns beyond the supported depth;
   END/inside-facing stations kept as depth-0 coupling nodes).
   5 new law tests.  The 156 m blade is DEAD; adjacent-ground tears
   0; nodes −331.
2. ★GOTCHA PROVEN AGAIN (bake chainid11): TWO sub-µm pairs = 673k
   triangles in one cell — sub-µm does NOT reliably collapse at .11f.
   Root: a multi-way collinear seam (junction+apron+4 strips+wall on
   one line, interleaved node subsets) where the nid-weld's figure-8
   membership guard SKIPPED insertion.  FIX: COORDINATE-TWIN nid —
   when the way already holds the node elsewhere, insert a twin at
   the same canonical lat/lon with copied claims; the mesh keys nodes
   by exact coordinates so the chains weld into one vertex, the OSM
   ring stays duplicate-free.  Bake chainid12: 23,448 airport
   triangles (healthy floor), near-parallel 1 (= baseline's own).
   CURRENT INSTALLED TILE = chainid12.
3. LEGACY DELETION — GATE LANDED (pipeline,
   O4_LEGACY_SURFACE_CLEARANCE, default ON), FLIP BLOCKED: CYXY
   gate-off measured nodes −2,242 BUT ways 460→817 (bands fragment
   over the vacated terrain), tears 0→4, post-weld crossings 3→24,
   within 77.  The bands' corridor march is the wrong tool for the
   vacated between-pavement regions — SLICE B's gap-fill/spine must
   own them first.  Sequencing confirmed: slice B → then deletion.

## SLICE B PILOT — GAP-FILL + DRAINAGE SPINE: SHIPPED ("build it")
* NEW MODULE src/auto_patch/gap_fill.py (Opus-implemented to spec) +
  pipeline wiring AFTER skirts / BEFORE bands (gap shapes join the
  bands' static union → corridor march skips covered frontage) +
  config gates (O4_GAP_FILL_SPINE default ON, GAP_FILL_SPINE_STEP_M
  15, GAP_FILL_MAX_WIDTH_M 160, GAP_FILL_MIN_AREA_M2 100) + 5
  synthetic tests (tests/test_gap_fill_spine.py).
* MECHANICS: enclosed gaps = INTERIOR RINGS of the airside pavement
  union (boundaries are pavement chains VERBATIM — chain identity
  free); spine = widest-cross-section midpoints every 15 m along the
  long axis, split into two faces sharing the spine chain; spine
  values = drainage solve inside the INTERSECTION of both parents'
  grade_law.adjacent_ground_envelope corridors (target ceiling−25 %,
  20 relaxation sweeps, endpoints pinned to pavement reads).
* ★GOTCHA: shapely ops.split with a cutter ENDING exactly on the
  ring returns 1 face (unsplit, all 3 CYXY gaps) — OVERSHOOT the
  boundary by 2 m; the faces carry the exact GEOS crossing points.
* CYXY: 3 gaps → 6 spine faces (38-84 verts each, mostly adopted
  chains); band polygons 156→127; audit unchanged (6 T / 1
  near-parallel = baseline).  BAKE: **23,052 airport triangles** —
  best of the day (pre-weld baseline 26,727), zero hotspot cells.
  INSTALLED TILE = gapfill2.
* Known approximation (flagged in module): a parent runway's code
  NUMBER derives from its longest vertex chord (no rw_axes plumbed) —
  revisit when slice B moves construction pre-solve.

## GAP-FILL EXTENSION ROUND (Noah: the 3 emitted gaps are PERFECT;
## why not all of CYXY's holes?)
* CENSUS (27 enclosed holes at CYXY): 3 emitted · 13 blocked ONLY by
  legacy surface_clearance strips inside · 8 blocked by building pads
  inside · 1 over the width cap by 4 m (163.9 vs 160) · 2 slivers.
* SUPERSESSION EXTENSION BUILT (wholly-inside legacy strips removed
  when their gap emits — whole-piece drop, chain-safe; partial
  straddles still block): 8 more gaps emitted, ways 460→417 — BUT
  near-parallel 1→4: (a) a 96 mm sliver between the spine's terminal
  segment and the boundary at hole 22 (shallow-landing trim built,
  did NOT clear it — root not yet confirmed), (b) 2× 7 mm pre-
  existing junction~junction seam divergences that the legacy weld
  insertions were incidentally pinning.  Per the zero-lens law the
  extension shipped GATED OFF: O4_GAP_FILL_SUPERSEDE default 0.
* SHIPPING CONFIG (installed tile): 3 gaps, near-parallel 1 (=
  baseline), **23,038 airport triangles**.  Face-boundary divergence
  snap + shallow-landing trim are in the module (harmless when the
  gate is off).

## OPEN-WAY SPINE REDESIGN (Noah's keyhole insight, round 2) — SHIPPED
Noah: don't cut the spine to the pavement edge — stop it short.
Evaluated: the keyhole variant works but its slit is a deliberate
near-parallel pair (safe only >= 0.5 m); the OPEN-WAY variant is
strictly better and uses the PROVEN crown-spine mechanism — the gap
emits as ONE polygon (ring = pavement chains verbatim EVERYWHERE it
touches pavement) + the spine as a floating interior OPEN constrained
way (o4_feature=gap_drainage_spine, layout.gap_spines → the crown
block in to_osm), ends held >= 2 m off the ring, ends take their own
corridor target (no pavement pinning — the mesh lerps spine-end →
boundary).  No split, no landing, no keyhole rails; U-shaped /
partially-open gaps need nothing special.
* RESULT: the 96 mm landing sliver died BY CONSTRUCTION and the two
  7 mm junction~junction pairs vanished with the split-face geometry
  — SUPERSESSION now audits at the absolute floor (near-parallel 1 =
  the baseline's own legacy pair) → O4_GAP_FILL_SUPERSEDE flipped
  DEFAULT ON.  CYXY: 11 gaps → 14 faces + spine ways, 10+ legacy
  strips superseded, ways 416, nodes 9,373.
* BAKE (installed tile): **22,836 airport triangles** — best of the
  day (pre-weld baseline 26,727; the whole day's arc: 3.0M → 22.8k).
* ⚠ PARALLEL-SESSION INCIDENT: config.py was reverted to HEAD by the
  other session mid-round, deleting the DAYLIGHT_SLOPE_LIMIT +
  GAP_FILL_* constant blocks (every import broke).  RESTORED from
  the agents' reports.  The same-checkout hazard is real — commit
  slice A+B soon, stage explicitly.

## THE PAVEMENT-NODE RULE (Noah, round 3): grading shapes NEVER
## create a node on a pavement edge — SHIPPED, plus chain-aware
## final decimation.  BAKE: **12,652** airport triangles (LESS THAN
## HALF the 26,727 pre-weld baseline, WITH full grading coverage).
* THE RULE: wherever a grading shape touches pavement its chain is a
  SUBSEQUENCE OF EXISTING PAVEMENT VERTICES — a mid-edge value is the
  lerp between pavement vertices, identical on both sides by
  definition.  Implemented: bands extend run-ends to bracketing ring
  vertices (adjacent_ground, both builders); skirts keep exact
  pavement corners at weld transitions (clearance, Opus — fallback +
  live report when the corner lookup misses); legacy strips thinned
  per-pass (Pass A fallback/A2/A3/B thinned; the Pass-A centerline
  raycast rows + Pass-C RESA synthetic rows are NOT ring
  subdivisions — left dense, documented).  105 tests green across
  the four suites.
* MID-EDGE CENSUS answered Noah's question: of 4,698 mid-edge
  pavement vertices, 4,470 were 3D-REDUNDANT (XY-collinear AND on
  the altitude lerp) — pinned by welds/adoptions AFTER emit
  decimation ran.  NEW: CHAIN-AWARE FINAL DECIMATION in to_osm
  (after the nid-weld, on final coordinates + consensus values): a
  COORDINATE (grouping coincident twin nids — one-sided twin removal
  is an exact-T-vertex mint, measured 43) is removed only when
  3D-redundant in EVERY referencing way with ALL ways agreeing on
  the leftover chord, then removed from all simultaneously;
  ring-degeneration vetoes globally.  Genuine wall twins (Δalt >
  0.1 m) and profile nodes stay.
* RESULT: pavement vertices 6,468→2,055; total patch nodes
  9,373→4,112; audit T=5 / near-parallel=1 (baseline's own); bake
  12,652 airport tris, hotspots ≤84/cell.  INSTALLED TILE =
  noderule5.
* Legacy-off RE-TEST with gaps live: still blocked (tears 7,
  crossings 40, ways 759 — the corridor bands fragment on vacated
  OPEN-terrain frontage; enclosed holes are no longer the blocker).

## IN-SIM REVIEW ROUND 4 (Noah: "definitely much better") — 3 questions
1. TWO STRIPS along taxiway outer edges = THE LAW'S ZONES, by design:
   the narrow strip is zone 1 (3 m drainage lip, 1.5-3 % down), the
   larger is zone 2 (graded band to the strip half-width, ≤5 %) —
   piecewise-linear corridor law → one two-row slab per linear piece.
   Slice B (solver spine) can unify them into one shape later.
2. SEAM DIPS (60.7203854,-135.0788903 + 60.7208756,-135.0791845):
   DIAGNOSED not fixed — the first coordinate is exactly vertex -5167
   (694.70) in a zone-2 band: a 0.72 m jog pair sitting ~25 cm below
   the line the pavement edge implies, at an intra-band RUN SEAM.
   Suspect: the run-end taper convention (borrowed run-end reference
   altitude instead of the local edge read).  NEEDS an emitter-side
   trace vs the corridor expectation — Noah's invariant stands: the
   shadow rows must mirror the pavement line exactly.
3. UN-FILLED HOLE at 60.7132799,-135.0645661 (census hole 27,
   72k m²): TWO stacked causes.  (a) width 163.9 > the 160 cap —
   FIXED, GAP_FILL_MAX_WIDTH_M 160→175.  (b) the REAL blocker: a
   HAIRLINE ENCLOSURE LEAK — at gap-emit time a sub-mm seam gap
   between two bounding pavement shapes leaves the hole topologically
   OPEN (not an interior ring; the candidate log proves it never
   reaches the gates), and the later nid-weld closes the seam so the
   FINAL geometry shows a closed ring.  Same family as the
   junction-seam pairs: a pavement-partition micro-defect; the
   doctrinal fix is PRE-SOLVE pavement conformance (slice B), NOT a
   detection buffer (which would perturb the verbatim boundary).
   Gap-fill now logs every candidate + skip reason (no silent skips).

## IN-SIM REVIEW ROUND 5 (pavement deformation + hangar violations +
## groundside ruling) — fixes + one open remover hunt
* USER RULING: no clearance/grading strips around GROUNDSIDE pavement
  (it follows the DEM — it IS terrain; welding law strips onto its
  rings imports conflicting values).  SHIPPED: groundside leaves the
  exact static unions of bands + legacy strips (no welded coordinates
  against it) and blocks them via a 1 m buffer instead; groundside
  excluded from band snap targets + the value-registry preload.
  BUILDINGS joined the standoff (measured: a legacy strip vertex 1 m
  from the building8 pad corner carried a 3.69 m foreign value —
  hangar-area worst pair now 1.40 m).
* USER RULING: pavement edges keep nodes on straight sections so the
  solver/mesh holds the edge grade.  SHIPPED in layers: MAX_CHORD
  60 m caps in BOTH decimators (emit_decimate._span_ok — the
  Douglas-Peucker span drop — and the to_osm chain-aware pass;
  O4_DECIMATE_MAX_CHORD_M) + conformance.densify_long_edges called
  PRE-SOLVE (226 vertices → real solver nodes at CYXY), at the emit
  stage (before decimation/projection, ordering law) and as the
  ABSOLUTE-LAST layout pass.  ⚠ OPEN: ONE 1,057 m junction chord
  (junction #101, nids -1692→-1785) STILL survives — the layout ends
  clean (final densify inserts 0) so the remover lives INSIDE to_osm;
  suspects: the chain-consistent needle removal (no chord cap) or an
  uncounted nid-drop path.  NEXT: instrument to_osm for removals on
  that way.
* Bake (installed): 15,726 airport triangles (the +3k vs 12,652 =
  the densified pavement nodes — the point of the ruling); audit
  T=5 / near-parallel=3 (the 3 = pre-existing legacy pairs).
* Round-5 residuals: hangar 1.40 m pair (attribution pending — may
  be a lawful apron↔groundside designed step) · seam dips (item 2,
  round 4) still open.

## OPEN (part 34)
* to_osm remover of densified nodes on junction #101 (above) —
  instrument, cap, re-verify the 60 m rule holds end-to-end.
* SEAM-DIP TRACE (item 2 above): run-end taper values at intra-band
  run seams — emitter trace, then the fix.
* HAIRLINE ENCLOSURE LEAKS (item 3): find + close the sub-mm pavement
  seam gaps pre-solve (the slice B pavement-conformance arc); hole 27
  fills once its seam closes.
* BUILDING-PAD PARENTS (8 holes): designed, not built.
* Legacy deletion (blocked on open-frontage band quality) ·
  full-suite re-triage · fixture recut after approval · spine nodes
  into the ONE solver graph (docs/chain_identity_one_solve_plan.md).
* 8 post-weld CROSSINGS (fixed coordinates, legacy classes — the
  residual report lists them; slice-5 legacy deletion owns most).
* Residual T-vertices 8 (legacy taxiway_clearance/groundside/wall
  classes, 1-15 cm) — currently harmless at mesh level.
* Skirt check_grade counters (within/skirt-edge from the advanced
  profile + weld rows) — VALUES work, ruled by "pavement wins";
  not mesh-blocking.
* Zone-3 smooth-blend-between-parallel-pavements (ruling 3) — needs
  the corridor-facing-corridor law; natural slice-B work.
* Slice B: pre-solve construction + solver absorption + perf levers
  (10 m stations candidate, coarser decimation, flatness skip).
* NOAH: fly the baked +60-136 tile (already installed).  On visual
  approval: commit slice A + recut fixtures.  ⚠ a parallel session
  is active in this checkout (tunnel/bridge research) — stage
  explicitly, never git add -A.

# STATUS — SESSION 20260709 (part 33): WELD RULING (terrain strips
# fully weld to pavement — the 1 m standoff grooves WERE the CYXY
# in-sim cliffs) + SKIRT GOVERNED FOOTPRINT ANCHORED AT THE RUNWAY END
# (blast pad inside it — the "~70 m too long" report).

## THE TWO USER RULINGS (Noah, in-sim review of the 20260708 bakes)
1. WELD: the adjacent-ground bands (and every terrain-grading strip)
   fully weld to the pavement they grade next to — no standoff gap.
2. SKIRT LENGTH: skirts read ~70 m too long past the runway end at
   multiple airports.

## DIAGNOSIS (measured against the BAKED +60-136 mesh, 21:18 bake)
* 620 near-vertical mesh edges (>2 m drop over <3 m) around CYXY.
  Families: 225 band↔strip cross-shape 1 m grooves · 193 band
  outer/inner edges (zone-3 faces up to 10 m — lawful by design, open
  question below) · 119 pavement↔strip grooves (worst 11.9 m =
  pavement↔skirt at the 14L end) · 29 coverage misses · 24 open
  terrain.  The 1 m `_PAVEMENT_GAP_M` standoffs left ribbons of RAW
  DEM that render as knife-edge blades (transects show +2.6 m and
  +5 m DEM spikes INSIDE 1 m grooves beside flat graded surfaces).
* Skirt length root cause: governed length applied from the PAVEMENT
  EXIT (blast-pad end); FAA AC 150/5300-13B §3.16 measures the safety
  area from the RUNWAY END with the stopway INSIDE it.  KCLT 18R:
  124 m pad → fill to 429 m past the end vs the lawful 305.  HECA
  pads 59–71 m = the observed "~70 m".

## LANDED (part 33)
1. SKIRT ANCHOR (lockstep): grade_law.
   runway_end_governed_length_beyond_pavement_m +
   runway_end_skirt_floor_profile_beyond_pavement +
   _breakpoints_beyond_pavement (profile ADVANCED by the overrun
   length; fill starts flush at the exit, descends at the advanced
   grade).  Consumers clearance._emit_one_end +
   verification.check_runway_end_skirt.  A pad longer than the
   footprint zeroes the skirt (CYXY 02/20 end 20: 154 m pavement past
   a 60 m footprint → skirt gone).  7 new law tests; STANDARDS.md row.
2. WELDS: adjacent-ground bands, runway-end skirt and legacy
   surface_clearance strips emit their inner row AT the pavement edge
   (d = 0) with the pavement edge values VERBATIM; ALL clips exact.
   Mesh-safe: O4_Vector_Utils.insert_edge splits constrained edges at
   encroaching nodes (z along the OLD edge) + the final
   enforce_conformance(0.01) weld.  CYXY grooved 2 m frontage samples
   6,473 → 119 (98% closed).
3. FALLOUT FIXES (each found by the validator chain):
   * legacy _finalize decimation (0.3 m band) + morphological open
     bulged welded rings ONTO pavement (29 m² overlaps) → re-cut each
     final piece against static_union post-decimation; overlap guard
     on _merge_coincident; _decimate(keep_predicate=) protects
     vertices ON the static boundary (wedges 168→92).
   * to_osm consensus mean let strip values MOVE runway ring vertices
     (within 0→661, worst 42% inside a runway) → AUTHORITY-AWARE
     consensus in layout.to_osm: nodes with any pavement/solver claim
     average AUTHORITY claims only; soft receivers (graded_strip,
     clearances, retaining_wall, boundary) adopt.  Within 661→43.
   * skirt end-strip weld rows span ±strip-half-width: only vertices
     actually ON pavement take the local read; off-pavement keeps the
     ref-anchored floor.

## ⚠ WELDS NOT SHIPPABLE YET — TILE MESH RUPPERT EXPLOSION (the gate)
* CYXY tile bake A/B: airport-region triangles 26,727 → 1,552,854
  (58×; tile 636k → 2.16/2.24M across two fix rounds).  Hotspots
  (25 m cells up to 263k tris): the 14R-end junction#90/strip#263
  seam + the apron#61/building8 seam.  Hotspot triangles are µm-to-cm
  scale with ZERO vertical steps → pure epsilon-GEOMETRY encroachment
  ping-pong (the KJQF class), NOT curvature: welded seams share
  vertex chains, and any post-clip mutation that moves/removes ONE
  side's vertex (emit sliver repairs, decimation chord cuts,
  _drop_sharp_corners, buffer(0) quantization repair, duplicate-nid
  drops) leaves a near-parallel constrained pair that Triangle4XP
  refines to machine epsilon.
* Three mutation sources were fixed (legacy re-cut + merge guard +
  decimate keep-predicates + chain-consistent needle removal in
  to_osm) — the wedge tripwire still reads 27→55 across iterations:
  patching mutations one-by-one does NOT converge.  NEXT SLICE (the
  real fix): CHAIN IDENTITY BY CONSTRUCTION — every shared boundary
  derives from ONE canonical vertex chain (the partition doctrine);
  post-emit mutation of any welded ring is forbidden (repairs must be
  chain-aware or pre-emit).  Until it lands the weld work stays
  UNCOMMITTED in the working tree; the cached CYXY patch was
  regenerated PRE-WELD (foreground-atomic stash A/B, wedge audit 0)
  so tile bakes REUSE it safely.  DO NOT bake with
  O4_AUTO_PATCH_REBUILD=1 on this tree.
* check_grade at CYXY weld5: within 43 (was 0: ~4 runway pairs =
  crown-domain bookkeeping at conformance-INSERTED nodes, the A2
  crown-blind-insert class, checker-side; rest sub-0.5 m soft↔soft
  seam steps) · skirt-edge 14 (law-vs-law CORNER ARBITRATION: skirt
  lift value vs band corridor value at shared coordinates, ≤0.4 m —
  needs the adoption rule: earlier strip's value wins) · steps 1+4 ·
  patch vertices 9,261→19,602.
* PATCH-LEVEL WINS (they survive once chain identity lands): grooved
  frontage 6,473→119 samples (98% closed) · overlap 0 ·
  adjacent_ground DEM reader 0 · tears 0 · authority values
  protected by the emit consensus rule.
* Zone-3 band outer edge still ends in a lawful VERTICAL face where
  the DEM is far below (193 mesh edges, ≤10 m).  NEEDS NOAH RULING:
  keep (regs: cliffs lawful beyond the graded band) or a render-only
  daylight taper at a natural slope.
* Legacy grooves not covered by any strip: 119 samples (~240 m).
* Compare-target fixtures (SPJC/SPLP) will drift from the weld —
  deliberate re-cut needed per policy (await sign-off).
* Fast suite 8 red: 4 known named + compare_target_splp ×2 (weld
  drift) + test_dsf_object_buildings flag-gating (PRE-EXISTING: the
  4ad87b8 default flip without the test) + test_cyxy_spine_zero_
  no_bowl building19 697.78 vs ≥698 (22 cm; attribution pending —
  likely the DSF default flip: rerun with O4_DSF_OBJECT_BUILDINGS=0).
* THE SKIRT ANCHOR FIX IS INDEPENDENT AND SAFE TO COMMIT SEPARATELY
  (grade_law beyond-pavement functions + the governed/floor lines in
  clearance._emit_one_end + the verification mirror + 7 law tests +
  docs — no mesh-density interaction; 68/68 skirt tests green).

# STATUS — SESSION 20260708 (part 32): RUNWAY DE-SEG MERGED TO dev +
# DEFAULT ON (O4_RUNWAY_SINGLE_POLY=1) + deliberate fixture re-cut
# (Noah sign-off).  Gates green; to-zero worklist A1-A5 in flight
# (docs/runway_single_polygon_plan.md Addendum 2 is the worklist).

## ADJACENT-GROUND LAW ARC COMPLETE — DEFAULT ON (babf296): SUITE 5
Full arc same-day: 03dc527 law (corridor constants + envelope, 33
tests) · 895dc4e emitter slice 3 (gate off) · d2f8f8c validator
(DEM transect reader + OSM tear sentinel; DEM-free corridor check
measured UNSOUND 200-1100 false flags → tear-scoped) · 2e2df5b
emitter round 2 (clamp-INTO-corridor replacing the skirt FLOOR
convention — 108-145% band internals → 0; triangle diet KCLT
283.8k→75.6k / HECA 235.4k→203.9k accepted <210k; strip declaw;
coverage 0; tears 0; + Noah directive: FULL boundary-ribbon
supersession gate-on) · 430c60a 30m parallel-merge shipped OFF
(over-couples genuine terrain; HECA wall RESOLVED at HEAD, tear
worst 31%→4.2%) · 5b88720 validator live-counter fix (driver
swallowed a TypeError → production adjacent_ground read 0
unconditionally; _GEOM_EXC narrowing) · babf296 DEFAULT ON + SPLP
fixture re-cut (boundary rows removed by construction, graded_strip
64/90, floors 0.95/runway-EXACT).
* FIRST FLIP ATTEMPT BLOCKED correctly (triangles 283.8k KCLT /
  wedges +3 / corridor violations) — the round-1 emitter used the
  skirt convention; the day-old validator caught it. Lockstep works.
* Suite gate-on 5 = the 6 MINUS pavement_grade[CYXY] (the law
  resolved apron #29 — CYXY within 1→0). Gate-on improvements:
  KJQF within 104→88, KSVH 5→0, KCLT break 8→2, KEXX 1215 m²
  bridge-overlap class DEAD, boundary+bridge 0 at all 8 airports.
* Follow-ups queued: KCLT/KEXX 1 un-filled junction-band residual
  each · cross-tile seam-column reader limitation (SPLP 5 per-tile
  findings at lon −77.000, not missing earthwork) · SPJC fixture
  re-cut to guard graded_strip · test_boundary 2 permanent skips
  retire with the bridge-deletion slice · enforce-fully trigger
  tuning (1 m under-enforces) awaits Noah in-sim · slice 5 deletion
  (bridges + ribbon + 30i tents + legacy chain + sub-rect crossing
  resolution) after in-sim soak · OLS follow-on arc.
* NOAH: restart Ortho4XP + bake +60-136 (CYXY plateau) — no env var.

## POST-WRAP CONTINUATION (same day): FULL SUITE 8 → 6
* 1ccd29f near-miss building frontage (S2): the SPJC pad↔apron 0.68 m
  DSF-vs-apt.dat source offset sat just past SHARED_VERTEX_TOL_M in
  ALL THREE reconcilers; fix = raise-biased soft anchors + law edges
  toward the already-chosen pad seat (per-EDGE recognition — the
  solve-time apron ring is sparse; stitch-tolerance widening REJECTED
  to keep the 0.5 canonical identity).  SPJC steps 5→0,
  pavement_grade[SPJC] GREEN; HECA building25 (0.81 m) also fixed →
  pavement_grade[HECA] GREEN — verified standalone: the 27-step
  service wall was NEVER test-visible (svc_break quarantine); the
  test's real blockers were the proximity radius + this one step.
  The wall stays open as an IN-SIM item (physical gates), agent on it.
* PARALLEL-ROAD WALL (part 30m OPEN (a)) — RESOLVED + candidate shipped
  OFF (anchors._parallel_station_merge_pairs, O4_SVC_PARALLEL_STATION_MERGE,
  default OFF; +12 tests).  Re-baselined at HEAD: the documented #576↔#584
  site is GONE (off-source SOURCE CLIP + adjacent-ground reshaped HECA's
  service net); the equivalent HECA pair is now 0.19 m (< the 0.5 m step
  threshold — 0 check_grade steps/cross airport-wide; the 0.845 m only
  survives O4_SVC_SPINE_FIRST=0 per-vertex).  Candidate (a) (widen the
  spine-station merge to ≤7 m with a tangent-parallel guard) FIRES only at
  CYXY -10045↔-10195 (6.7 m apart) where the two roads differ ~1.5 m for
  GENUINE terrain reasons (non-overlapping reach bands — the SAME physics
  part-30m recorded for #576↔#584) → forcing a shared seed REGRESSED CYXY
  (service tear 22.2→23.2 %, facing step 1.523→1.587 m).  Proximity+parallel
  can't tell "coincidental wall that should be flat" from "terrain genuinely
  holds them apart" (identical geometry), so no guard makes it both effective
  and non-regressing; kept gated off for a future revisit carrying a co-level
  signal (shared groundside).  Default byte-identical (HECA/SPLP/CYXY alt
  multiset unchanged; fast suite = the same 4 reds).  Candidate (b) (<5 m
  cross-shape law) not pursued: the live pair is 6.7 m (out of its window)
  and a hard law would over-couple the same terrain more rigidly.
* 03dc527 adjacent-ground law slices 1+2 (behavior-inert): corridor
  constants + STANDARDS rows + grade_law.adjacent_ground_envelope
  (enforce-fully corridor semantics per Noah ruling 1) + 33 tests.
  Slice 3 emitter in flight (gated OFF, phased CYXY-first, HECA
  flat-airport corridor-cost checkpoint before any default talk).
* Remaining full-suite 6: no_self_overlap[SPJC] + route_band_zero
  [SPJC] (30l CHECKPOINT class) · pavement_grade[SPLP] (#66 5 cm/
  0.8 m pair) · pavement_grade[CYXY] · cyxy_route_reach ·
  solver_validator_same_edge_budgets.

## SESSION WRAP (2026-07-08 end): FULL SUITE 13 → 8, all named
Final tree c1c7a49.  Commits this session: 8c9fdc3 merge · 2a217d7
flip+re-cut · ff332e9 A4 · ec7f632 A6 · f86d7ee A2 · 6ac66cd A1/R1 ·
2da0ce3 A3 · 5782ab2 #336 A+B · 0388323 A8 crown-plane · 06b84aa B1
end-cap escalation · cc45410 Fix C source-clip · c1c7a49 proximity ·
+ docs (0e085b8 adjacent-ground plan, f27d896 gap audit).
* STANDALONE check_grade scoreboard: SPLP 0 · SPJC 0 · HECA 0 ·
  CYXY 1 (apron #29 +0.25%).  KCLT within 3, off-source 8→1.
* FULL SUITE 8 (was 13 at session start), every red precisely named:
  - pavement_grade[SPLP]: 1 within pair 5 cm/0.8 m per-tile junction
    #66 (unmasked by the proximity fix — cross assert no longer hides
    it).  NEW, small, weld-value class.
  - pavement_grade[HECA]: the 30m service-road parallel wall (27
    steps; owner identified — station merge widen or cross-shape
    service law).
  - pavement_grade[SPJC]: 5 building↔apron steps (worst 0.66 m).
  - no_self_overlap[SPJC]: 30l CHECKPOINT apron∩service clip
    (awaiting coordinator approval round).
  - route_band_zero[SPJC] (196) · pavement_grade[CYXY] (apron #29) ·
    cyxy_route_reach · solver_validator_same_edge_budgets (CYXY
    52/17649 apron/junction cm-noise) — the CYXY/SPJC solver arc.
* LEGITIMATE GREEN FLIPS this session: compare-target ×3 (re-cut),
  runway_longitudinal_grade[SPLP] (B1 — first since 30i unmasked),
  pavement_rests_on_source[SPLP] (Fix C killed a hidden 34k m²
  phantom #34), cyxy_taxi_e_south_apron (B1 flex-path cap threading,
  cm-scale lawful), vertical-curve XPASS SPJC/CYXY (genuine,
  gate-off-verified).
* MYTHS RETIRED: "HECA fails in suite, never standalone" = the test's
  proximity_m 1.0 vs the 0.5 weld tolerance (B2; surface
  byte-identical); "EB-109 EMAS doc" does not exist (gap audit);
  suite-context cache leakage (B2 checked all persistent caches —
  clean).
* A7 CLOSED benign accounting (99.6% break nodes identical; growth =
  denser vertices on pre-existing junction pockets).
* NEW DOCS: docs/adjacent_ground_grade_law_plan.md (boundary-bridge
  retirement; 3 decisions await Noah) + docs/grade_law_gap_audit.md
  (OLS/GS-plane/PVI-spacing/RSA-fine/helipad top-5).
* QUEUE (named, ordered): SPJC building↔apron steps · HECA service
  wall · SPLP #66 pair · KCLT #763 clearance remnant · 30l CHECKPOINT
  approvals (apron∩service clip; hole-aware conformance) · CYXY
  solver arc (route_reach, edge budgets, apron #29) · adjacent-ground
  law build (post Noah rulings) · gap-audit top-5 · Section C
  cleanup (post in-sim soak).

## LANDED (part 32)
1. runway-deseg → dev FAST-FORWARD (dev @ 8c9fdc3; dev was a strict
   ancestor — merge conflict-free by construction, as verified in
   Addendum 2).
2. DEFAULT FLIP + FIXTURE RE-CUT (2a217d7): config.py
   RUNWAY_SINGLE_POLY default "0"→"1"; SPJC + SPLP compare-target
   fixtures re-cut with tools/build_target_osm.py.  Runway ways
   SPJC 35→2, SPLP 9/8→1/1 per tile.  Gate-off CONTROL builds
   attribute every non-runway delta: SPLP-78 runway-only; SPLP-77
   junction 20→27 + SPJC junction 289→321 / taxiway_clearance 27→24
   = the neck-split corridor re-evaluation cascade responding to the
   one-ring runway; the apron 100→44 / junction repartition vs the
   07-06 fixture is 30k/30l/30m dev drift absorbed by the same
   re-cut.  Floors 0.95×current EXCEPT runway = EXACT (deterministic
   ring count IS the de-seg invariant; 0.95 of 1-2 ways guards
   nothing).  compare-target 3/3 green.

## VERIFIED (default-on gates, this session)
* fast_suite: 5 = the 8 minus compare×2 (legitimate re-cut absorb)
  minus runway_longitudinal_grade[SPLP] — a FALSE absorb (A4 below).
  FULL suite: 9 = the 13 minus compare×3 minus the same false absorb;
  ZERO new failures.  test_runway_vertical_curve XPASS at SPJC/CYXY/
  HECA = the SAME A4 dark spot (2-end rings give the curvature check
  nothing to measure); SPLP's stays correctly XFAIL (seam-split rings
  carry interior seam vertices).
* check_grade: SPLP within 31 (ALL ≤+0.11% at-cap marginal class —
  awaiting the A3 scoping ruling, NOT re-baselined) · CYXY within 1
  (pre-existing apron #29) · HECA within 2 (the A2 pair @3.57%,
  junctions #215/#226 beside 05R) · plane/cross/skirt/steps 0
  everywhere (HECA vertex/mid-edge steps = the known 30m service_road
  classes, unchanged).
* wedge_audit: CYXY 0 (target met) · SPLP 0 · HECA 4 (no growth).
* Verify-log HECA same-session ON vs OFF: +2 sub-mm junction~junction
  wedges (the A1 frontage class), −1 clearance∩clearance sliver, −1
  OFF-SOURCE phantom (30l's apron #244 30 m² @05R ABSORBED by the
  ring).  12 vs 12 total; no new classes; ZERO runway-family findings.
  SPLP + CYXY verify all-zero (SPLP runway_grade 4→0 = A4 dark spot,
  not a fix).
* flex_audit HECA de-seg parity (ON vs OFF, both flex-on): 4/158
  matched runway nodes differ, ±0.41 m max, at the two inter-runway
  reconciliation spots — flex law equivalent.  Flex-on vs flex-off
  map: ±4 m at-budget flexes, binding taxi axes at/over cap
  (flex-last holds; the one "+0.06% slack" is sub-noise).
* Dip probe: dead by construction (part 31 — no interior cross-edges
  exist under the gate; 30i tent pass structurally no-op).

## OPEN (part 32 = Addendum 2's OUTSTANDING list) — MID-SESSION UPDATE
* A4 DONE (ff332e9): check_runway_profile per-station on rings +
  crossing-slab phantom exclusion (materialized at CYXY, excluded by
  station not noise).  SPJC/CYXY vertical-curve XPASSes proved
  GENUINE (gate-off ground truth 0) — only HECA's returns to xfail.
* A6 DONE (ec7f632, found by A4's agent): seam split dropped
  from_single_poly — SPLP built HALF-DE-SEG (join anchors +
  corner reads + profile check all legacy).  One-line propagation;
  fast_suite 6 = the 5 + runway_longitudinal_grade[SPLP] correctly
  RED again.  A chip-spawned duplicate session may exist — the fix
  is already in.
* A2 DONE (f86d7ee): HECA within 2→0.  Root = the final
  enforce_conformance weld interpolating crown-UNAWARELY across a
  crown discontinuity (solver value was lawful); fix = shared
  insert-altitude rule: coincident-ADOPT for soft receivers (value
  authorities never adopt) + crown-aware z' lerp on exact canonical
  nodes.  Bonus: KCLT within 9→5, SPJC suite cross 9→0.
  pavement_grade[HECA] stays red on a PRE-EXISTING suite-context-only
  cross divergence (byte-identical A/B at bare HEAD; the known
  "standalone probes never reproduce" HECA gap) → section B.
* A1 EVOLVED (5 diagnosis rounds, 2 designs measured-and-rejected):
  ring≡legacy contour (cm); real root = _enforce_shared_vertices
  cluster-MEAN placement (pavement/vertices.py:1166) — no runway
  vertex anchors frontage clusters → merged verts land 0.27 m off
  the ring chord (KCLT), 14 mm (SPJC, a 4→14 mm knife-edge flip past
  the 10 mm weld tol), and ring stations drift 3 cm.  R1 IN FLIGHT:
  runway-anchored canonical points (runway vertex wins; runway-near
  cluster means project onto the runway boundary; two-authority
  clusters freeze).  GATE-INDEPENDENT — fixes legacy too (gate-off
  twin #792 same defect); suite-verified, byte-identity waived per
  the correctness-work rule.  Emit-stage mop-up pass preserved in
  session scratchpad ring_frontage_pass/ (superseded if R1 holds).
  R2 (mixed-regime strip: KCLT #344 internal 0.2 m steps, SPJC #141
  plane pair — slice-minted sliver, one shape/two value authorities,
  BOTH gates) decided after R1's residual numbers.
* A5 DONE: KCLT triangles 49,952 vs 130,468 (−61.7%), same-session
  A/B, runway ways 3 vs 7.
* A3 DONE (2da0ce3): grade_law.runway_within_pair_in_domain
  (station clustering 5.0 m, |Δstation| ≤ 1) applied ONCE in
  grade_graph.plane_constraints — both readers lockstep by
  construction (check_grade passes o4_single_poly from the new
  additive way tag; solver/in-memory builders exclude runways so the
  scoping is latent-but-identical there).  SPLP within gate-on
  31→12 (19 multi-station chords left; check_runway_profile still
  reports the real 1.78%/1.52% — the ruling's point); gate-off
  18→18 byte-identical no-op (fresh baseline is 18 not the doc'd 16
  — DEM-state dependent; predicate gated on single_poly because a
  short/wide SEGMENTED rect's diameter axis is diagonal and would
  mis-station).  Residue: 8 runway same/adjacent pairs (4 short
  lateral + 4 long 485 m adjacent-station chords at sparse flat
  ends) + 4 junction-way mirrors (out of ruling scope; does not
  dominate → no checkpoint).
* A7 NEW: HECA break growth 5891→6176 at gate-on is NOT A2's root
  (pair >1.5 km from any break region) — solver-time, own trace,
  after A3.
* KCLT #336 PHANTOM (Noah in-sim report post-bake) FIXED (5782ab2):
  slice EXONERATED (faces born 100% on-source); the 24.7k m² @31%
  junction spanning 18L = _enforce_runway_1to1_sharing's
  straightening chord sweeping grass + its off-source carve
  FALLING BACK on a GeometryCollection (split-keep handled only
  MultiPolygon — the recurring shapely-2 class); the 0%-source
  sliver cluster = route-proximity-cut pieces shielded from
  _drop_off_source_residue by the rpc flag.  Fix A: polygonal-parts
  filter in the carve; Fix B: near-zero on-source drop precedes the
  rpc exemption.  KCLT off-source 8→2 (#336 GONE; region now apron
  @95% + junctions @100%); HECA off-source 1→0 (the 30l apron #220
  phantom dead); all canaries byte-identical; suites 6/10; no
  real-source piece dropped (all enumerated ≤0.2%).  Provenance:
  the CLASS predates R1 but #336's face was R1-reshaped (R1 shrank
  the old −80.966 giant and the chord moved to 18L).  REMAINING =
  Fix C (formation-time source-clip for partial-coverage bands:
  KCLT #278 8253 m²@35% + #763 383 m²@32%) — own gated slice,
  candidate to bundle with the adjacent-ground law arc.
* BOUNDARY-BRIDGE RETIREMENT design SHIPPED as
  docs/adjacent_ground_grade_law_plan.md (0e085b8): primary-verified
  regs research (two-zone profile: 3 m drainage lip falls AWAY,
  bounded graded portion by role/code, then ≤5% UP cap only — NO
  downward mandate beyond the graded band = cliffs lawful; aprons
  have NO mandated area beyond the edge — wall lawful).  Law =
  lateral generalization of the skirt; 3 decisions awaiting Noah in
  the doc (FAA 1.5% minimum skipped, OMGWS keying, apron wall
  rendering).
* NOAH: bake after A1/A2 land (his call); restart Ortho4XP first
  (GUI caches auto_patch imports).
* Section C cleanup unchanged (after in-sim soak, byte-identical
  dead-code rule).

# STATUS — SESSION 20260707/08 (part 31): RUNWAY DE-SEGMENTATION —
# single-poly rings behind O4_RUNWAY_SINGLE_POLY (branch runway-deseg,
# docs/runway_single_polygon_plan.md; slices 1-5 landed, gate default OFF)

## LANDED (part 31, branch runway-deseg @ 6880c8b, base dev 773dcb9)
1. Phase 1 consumer inventory (cd5d3e4): table in the plan doc.
   Measured corrections: NO 100 m uniform grid exists (removed
   2026-05-22); the real emit surface is elevation.py's chain→
   BuiltShape conversion; profile_state carries everything a ring
   builder needs; crown rect-equalization + emit decimation are
   already ring-safe.
2. Emitter (4d41a40 + 00e68b3): ONE ring per runway ref from the
   persisted FAA profile (elevation._build_single_poly_runway_ring)
   — long-edge vertices at every profile station, per-node
   altitudes = profile(station); fully-flat profile keeps the flat
   altitude= form (MULTI_FLAT parity).  stitch_pavement_to_flat_
   runways learned per-vertex FLAT RUNS; stitch_pavement_polygons
   hosts the ring as a per-vertex peer; _build_runway_corner_
   altitudes reads ring corners.  All new paths keyed on
   BuiltShape.from_single_poly / the gate → gate-off byte-inert.
3. Seam (verified, no code): per-tile SPLP gate-on = one ring per
   tile (21+17 nodes vs legacy 9+8 pieces); worst cross-tile
   seam-gap pair IDENTICAL to gate-off (0.27 m/11.8 m, same vertex).
4. Joins/flex (26d9c4a): _runway_anchors on a ring samples the
   runway surface at the ANCHORED NODE's boundary projection (the
   ring's whole-profile interpolation otherwise pins the contact's
   station value 5-15 m up-axis onto a node 2.5 m from the weld —
   unlawful).  O4_DESEG_DEBUG=1 prints anchors.  flex_audit at
   HECA: identical gate-on/off (0 clusters both).
5. Crossings (6880c8b): axis-intersecting ring pairs carved at
   candidate stage — crossing junction = union of both refs'
   station SLABS over the overlap (cut lines pass exactly through
   station vertices), rings contribute remainder pieces, junction
   takes the legacy inverse-distance profile blend + '+' ref.
   Close-pass overlaps (no axis meeting) stay whole for the
   overlap-clip.  CYXY: 2 junctions carved, 02/20 → 3 pieces.

## VERIFIED (gate-on unless said; gate-off fast_suite = the 8 exactly)
* Runway way counts: SPJC 35→2 · HECA 56→3 · CYXY →7+2 crossings ·
  SPLP per-tile 9/8→1/1 · KJQF →1.  All per-node alt_abs.
* check_grade: CYXY within 1 == gate · HECA plane/cross/skirt 0,
  steps 3+14 == baseline · SPJC within 0 · SPLP plane/cross/skirt 0.
* WEDGES: CYXY 2→0 (junction~runway ELIMINATED) · HECA 5→4 ·
  KJQF 5→5 (all junction~junction) · SPLP 0→0.
* Part-30i tent machinery structurally no-op: HECA crown_centerline
  53→0 (no interior cross-edges exist); crown_spine ridges emit
  continuous (HECA 11→3 ways); crown_drops field intact.
* Segment-dip class: DEAD BY CONSTRUCTION under the gate (no
  interior cross-edge = no flat-across constraint anywhere).
* ISOLATED TRIANGLES (30g harness, /tmp/meshdiag): HECA 44,946 →
  43,110 (−4.1%, same-session A/B); KCLT gate-on 49,810 vs the
  130,614 recorded for dev at part 30j (−62%; same harness+tile —
  re-run the gate-off KCLT emit for a same-session A/B).
* KCLT gate-on: 3 rings (as many refs as profile_state pairs, same
  as legacy), 48 skirts, plane/skirt/cross 0, within 8 (baseline 6
  — the frontage weld class), wedges 12→10 (4 junction~runway of
  the SPJC frontage class remain).

## OPEN (part 31 — before default-on)
* HECA +2 within pairs @3.57% (9 cm/2.51 m beside 05R): junction
  vert takes the NEXT station's value; NOT a runway anchor (debug
  confirms) — suspect level/mesh coupling.  Break 5891→6176.
* SPJC +1 junction plane pair (2.30%) + one 16 mm runway~junction
  wedge: junction frontage vert 1.0 m from a ring corner (inside
  stitch snap_corner guard) never welds; legacy welded via
  canonical merges of per-station corners.
* SPLP within 16→31: same marginal ≤+0.11% at-cap class, more
  pairs (the ring exposes longer chords) — within-shape all-pair
  conflates longitudinal law (profile checker's domain) with
  lateral law on a ring.  Validator scoping decision WITH NOAH
  (the 30i centerline-exemption argument, extended).
* check_runway_profile clusters per-piece extreme stations → on a
  ring it sees only the 2 runway ends; needs per-station clustering.
* Fixture re-cut (compare-target counts) — AWAITING NOAH SIGN-OFF.
* KCLT gate-on + isolated-triangle A/B vs 1655550 (30g method).
* WORKING-TREE HAZARD: the parallel dev session commits in THIS
  checkout — 4d41a40 carries its clearance 30k fix (same content as
  dev 3d830ec; merge should auto-resolve); its 19feaec landed on
  runway-deseg.  Stage explicitly, never git add -A.
# STATUS — SESSION 20260708 (part 30m): SPINE-FIRST service-road grading
# (USER RULING 2026-07-07) — the truck-route SPINE grades at the road cap
# with DEM as a SOFT station seed; the EDGES follow the spine (cross-section
# derived, 2 % transverse law); a cross-road tear is now UNREPRESENTABLE.
# Base: dev@70ddd84.  Gate: config.SVC_SPINE_FIRST (O4_SVC_SPINE_FIRST,
# default ON; off = canonically identical emit to 70ddd84).

## THE DEFECT (reproduced first)
Part-27 DEM-follow (route_profile/anchors.apply_service_road_dem_follow)
was PER-VERTEX: every service node clamps its own DEM into ITS reach band,
so a road's two long edges bind to DIFFERENT anchor regimes.  CYXY probe
60.7092306,-135.0738928 (O4_PROBE_NODES): service_junction #64 DEM-weld
side 709.01 vs clearance-side solve 706.52/706.72 = a 2.49 m cross-road
tear on a ~6 m road (42-77 % transverse).  The tear was INVISIBLE to the
law: service_road was in neither SOFT_VISIBILITY_ROLES nor
junction_rules.SLOPING_RECT_ROLES → ZERO within-shape edges in
build_unified_graph; the validator's break-region quarantine (the 15
CYXY service_break nodes) masked it in the gate (WITHIN=1 counted only
apron #29).

## LANDED (part 30m) — three coordinated touch points, one gate
1. **LAW COVERAGE** (grade_graph.py): ``service_road`` joins
   ``SOFT_VISIBILITY_ROLES`` (gated) — the road body gets within-shape LAW
   edges through the SAME classify_pair/_bake_edge path as service_junction
   on BOTH readers (solver graph + validator import the same tuple):
   cL = SERVICE_ROAD_MAX_GRADE (5 %) along the route,
   cT = SERVICE_ROAD_MAX_TRANSVERSE (2 %) across it (the _bake_edge
   road-rate branch existed since 29b).  ``_body_cap`` gains the explicit
   service_road → road-cap branch (it would otherwise inherit a taxi cap
   from a welded neighbour via the junction fallback).
2. **CROSS-SECTION SAMPLING** (lateral_spine_nodes.insert_service_lateral_nodes
   + pipeline call after the taxi lateral pass): SERVICE centerline stations
   (densified to SPINE_STEP_M) project perpendicular feet onto
   service_road/service_junction edges — the law now binds ALIGNED
   cross-section pairs at station spacing instead of ring corners 70-100 m
   apart (the in-sim "ridge" report class).  The taxi lateral pass still
   skips SVC lines (aprons must not couple to the road law).
3. **SPINE-FIRST SEED** (anchors._svc_spine_station_seeds): DEM-follow is
   computed per spine STATION and shared by the whole cross-section:
   stations = clusters of ring-vertex projections onto the service lines;
   station DEM = member mean, LOW-PASSED along the line (±1.5 steps —
   raster noise at a lone unpaired station read as a 4.4 % diagonal pair);
   station band = INTERSECTION of member node-graph reach bands (same
   anchors/metric/connectivity as the per-vertex operator — an earlier
   station-graph Dijkstra draft left whole chains anchor-unreachable);
   clamp + the SAME distance-weighted break blend, marked through the
   existing service_break quarantine.  SEEDS ONLY: anchor (weld) vertices
   are never reseeded (mouth behaviour unchanged), no hard per-vertex
   clamps survive on edges — the law edges are the authority and the
   solve's projections (yield + final) remain the sole writer.
4. **STRICT-FRAME QUARANTINE ALIGNMENT**
   (grade_graph_validate.within_violations): the in-memory strict frame
   now excludes pairs touching a solver-exported ``_break_node_ll`` node —
   the SAME split ``check_grade.run_checks`` applies (user ruling
   2026-07-05/e2031ff: a solver-declared pocket's designed blend is
   reported separately, never counted actionable).  Needed because the
   frame predates service law coverage: with service_road pairs now
   checked, the quarantined descent blends (5.38 % vs the 5 % road cap +
   3 junction chords at 1.62-1.64 % welded into blend regions) read as
   "new" strict spine violations and flipped test_cyxy_spine_zero /
   test_cyxy_spine_zero_no_bowl RED with no physical change at those
   spots.  Scope is exactly the solver's own break export — the
   anti-gaming test (test_validator_detects_spine_step, injected 3 m
   fake step) still PASSES; empty export ⇒ byte-identical check.

## VERIFIED (gates)
* RULING PROBE (CYXY 60.7092306,-135.0738928): cross-section single-valued
  709.01/709.01 on BOTH ways (was 709.01 vs 706.52 service_road #203 and
  709.01 vs 706.72 service_junction #64); clearance ribbon follows the
  road edge (709.00/708.83, was 706.46/706.74).  Local spread 0.56 m over
  24 m (lawful ≤5 % longitudinal blend over the run).
* TEAR AUDIT (all service short chords <10 m over 5 %): CYXY worst
  76.6 %→11.9 %; the catastrophic class (>12 %) is GONE.  Roads -10193/
  -10194/-10203/-10060/-10192 cleaned to zero pairs.  Pair count 114→120:
  the residual pockets (-10205 49→66, -10202/-10206/-10201 ±) are the
  PRE-EXISTING ≤1.1 m break-blend descents at the same coords/magnitudes
  (worst 12.13 %→11.87 %), just carrying more measurable vertices from the
  lateral pass; the 3 "new" -10032 pairs are 5.8-6.6 % threshold-crossers
  whose local spread IMPROVED (0.32→0.29 m over 9 m).
* check_grade (test frame): CYXY WITHIN 1→1 (the same pre-existing apron
  #29 +0.25 % pair; the service tear pairs it replaced are now LAWFUL, not
  re-quarantined), CROSS 0→0, STEPS 0→0.
  SPLP: BYTE-IDENTICAL emit (0 service routes kept — scoping proof).
  HECA: WITHIN 0→0.  CROSS 10→8 and STEPS 17→27, EVERY delta
  enumerated (rider 4):
    - The #64↔#612 parallel-road WALL (30.101606,31.393602 /
      30.102273,31.394996): baseline = ALL 17 steps (0.59-0.92 m) + 3
      cross (80 %/0.80 m + 18 %/0.18 + 11 %/0.11).  After: 25 steps at
      LOWER magnitudes (0.55-0.69 m; max 0.92→0.69 — more measurable
      samples along the same, now-shallower wall from the lateral-pass
      vertices) + 2 cross (64 %/0.64 m + 10 %/0.10 — the 0.80 m worst
      REDUCED, one of three pairs eliminated).
    - #576↔#584 (30.108313,31.388292): baseline cross 18.31 %/0.16 m →
      96.12 %/0.84 m + 2 new 0.84 m steps (same two coords) — the ONE
      adverse delta.  DIAGNOSED (not fixed): ways -10575/-10583 are two
      NON-TOUCHING roads with a 1-7 m terrain gap; the baseline agreement
      was coincidental (both sides per-vertex-clamped nearly the same
      DEM); spine-first moved each road onto ITS OWN spine regime
      (#584 → 92.7-93.0 on its line's band, #576 stays on its welded
      94.8→93.9 descent).  No within-shape law exists BETWEEN shapes, the
      2 m proximity window correctly does not couple a 7 m rendered gap,
      and the solve-time seed debug shows no coupled nodes there.  Two
      candidate fixes queued (OPEN below); left honest — the aggregate
      CROSS still improved 10→8 inside an already-red pre-existing gate
      (pavement_grade[HECA] is one of the 13 pre-existing failures).
    - junction cross class (9.31 % + 4× 5.47 %, all ≤0.07 m): unchanged.
  HECA probes (3 spots, OFF→ON local spread): worst-tear 30.11064,31.39841
  0.63→0.42 m; -10106 30.11218,31.40624 0.95→0.88 m; wall covered by the
  step enumeration above.
  HECA tear audit: worst 33.4 %→31.0 % (same spot, dz 0.41→0.38 over
  1.23 m); pair count 48→146 and dz>0.5 m 6→46 — ALL the added pairs are
  ~10 m DIAGONAL chords at 5.6-7.5 % on the steep quarantined descent
  pockets (the part-30d "isolated roads over steep terrain" class), now
  sampled at station spacing; the sharp SHORT-chord step class shrank
  (see probes).  No new cross-road tears.
* wedge_audit HECA 5→4 (improved).  conformance HECA 39/2149 ramp 3 →
  40/2223 ramp 4: the 8 big clusters byte-identical; the single +1 is the
  SAME marginal spot 30.1075,31.4021 re-clustered (2 verts @+0.78 → 1
  @+0.80 + 2 @+0.58, threshold 0.5); ratio 1.81 %→1.80 %.
* break-region: CYXY break_ll 779→713 (svc_break 15→106); HECA break_ll
  11084→11428 (+3.1 %; svc_break 18→87).  The station blend quarantines
  WHOLE cross-sections of the genuinely-broken descent pockets instead of
  lone vertices, and the lateral pass added vertices inside those same
  pockets (CYXY +355, HECA more) — CYXY's NET quarantine still shrank,
  and the tear audits + probes above prove the quarantined surface no
  longer tears cross-road.  final-projection residual (CYXY 1→112
  over-cap edges, 10 both-hard): NOT comparable to baseline — baseline
  service roads had ZERO law edges at emit, so the projection could not
  see (or count) their surface at all; the residuals sit inside the
  svc_break quarantine (gate WITHIN=1 proves none actionable).
* wedge_audit CYXY 2→2 (no growth); conformance CYXY 35/1269 ramp 25 →
  18/1281 ramp 10 (IMPROVED — clearance cuts conform better once the
  road edges are regime-consistent).
* gate-off: O4_SVC_SPINE_FIRST=0 CYXY emit CANONICALLY IDENTICAL to
  70ddd84 (same node/way multiset; raw byte order differs run-to-run at
  HEAD already — verified logs identical modulo wall time).
* FULL suite: EXACTLY the 13 pre-existing failures (splp compare ×2,
  pavement_grade SPLP, runway_longitudinal SPLP, compare_spjc,
  no_self_overlap SPJC, pavement_grade SPJC, route_band_zero SPJC,
  cyxy_taxi_e_south_apron, pavement_grade CYXY, cyxy_route_reach,
  solver_validator_same_edge_budgets, pavement_grade HECA); 408 passed
  (+2: the two spine-zero tests below).  Fast subset of those failures =
  exactly the documented 8.  NOTE: the first full run had 15 — the two
  spine-zero tests flipped RED on the strict frame's missing quarantine
  (see LANDED #4); with the frame aligned they PASS and the anti-gaming
  injected-step test still PASSES.

## OPEN (part 30m follow-ups)
* HECA #576↔#584 (30.108313,31.388292; ways -10575/-10583): the one
  adverse delta (cross 0.16→0.84 m, 2 steps 0.84 m).  Candidate fixes:
  (a) widen the parallel-road STATION merge to gap ≤ ~5-7 m with a
  tangent-parallel guard (couple only near-parallel lines, not distinct
  crossing roads), or (b) a cross-shape service law edge for facing
  road edges < 5 m apart (the vertex-to-edge step check already measures
  exactly this pair — the law should too).  Both perturb HECA's solved
  service field → own gate cycle.
* CYXY -10205 / HECA steep-descent pockets: still >5 % short chords in
  the svc_break quarantine (the genuine contradictory-anchor descents,
  pre-existing).  The station blend renders them as single-valued
  cross-sections now; driving the quarantine itself toward 0 needs the
  mouth-anchor contradictions resolved (groundside reach / weld-level
  work, out of scope here).
* The parallel-road STATION merge (XY ≤2 m + node-prox pairs) measured
  as a strict no-op on CYXY/HECA final metrics (all v2/v3/v4 numbers
  byte-identical) — kept because it is the correct station-level
  analogue of O4_SVC_PROXIMITY_COUPLE and guards the <2 m sliver class
  (HECA #510↔#517) against regression under future station layouts.
* Emit-order nondeterminism (pre-existing at HEAD): two identical-env
  builds differ byte-wise in node-id assignment while canonically
  identical (same node/way multiset; verified 70ddd84 baseline vs two
  gate-off builds, canon sha 5a98230c9efc5edf).  Makes byte-diff gates
  noisy — worth a stable-sort at emit some day.

# STATUS — SESSION 20260707 (part 30l): VERIFY-LOG DRIVE-TO-ZERO across
# the 14-airport loop (5 fixtures + KCLT satellite family).  1 emitter FIX
# (fully-contained service_junction self-overlap); everything else
# classified KNOWN-OPEN (solver/slice-owned) or CHECKPOINT (needs review).
# Base: dev@3d830ec.  Fix committed on dev in the verifyloop worktree.

## THE FIX (fix class 1 — LANDED)
`groundside._deconflict_service_overlaps` clips the smaller of two
overlapping SERVICE shapes against the larger, but when the yielder lies
WHOLLY inside the kept shape the difference is empty, `parts` is empty, and
the old `continue` left the fully-covered yielder in the layout — a
100%-area self-overlap (KEQY service_junction #23, 109 m², entirely inside
#21).  FIX: drop the redundant yielder in the empty-parts branch (a
`drop_ids` set filters removed shapes at return; partial lenses still clip
as before).  KEQY verify overlap 1 → 0, coverage unchanged (kept shape
already covers the footprint at the same role).

## SCOREBOARD (verify_and_log findings; BEFORE dev@3d830ec → AFTER fix)
Columns: OVL=self-overlap  SRC=off-source  WDG=epsilon-wedge  RWG=runway_grade
```
airport   OVL      SRC     WDG      RWG     total      note
SPLP      0        0       0        4→4     4→4        RWG KNOWN (runway solver)
CYXY      0        0       2        0       0→0        (wedge audit only; verify 0)
SPJC      2→2      0       10       0       12→12      all KNOWN (slice + carve)
HECA      5→5      1       9        0       15→15      all KNOWN
MMOX      0        0       0        0       0          clean
KCLT      8→8      10      11       0       29→29      all KNOWN (slice + off-source)
KJQF      3→3      0       3        0       6→6        all KNOWN (bld-hole + slice)
KSVH      1→1      0       1        0       2→2        KNOWN
KEXX      2→2      0       0        0       2→2        KNOWN
KVUJ      2→2      0       0        0       2→2        KNOWN
KEQY      1→0      0       4        0       5→4        OVL FIXED (this session)
KRUQ      0        0       0        0       0          clean
KAFP      0        0       0        0       0          clean (tile +35-081)
```

## CLASSIFICATION (every non-zero finding)
FIX (landed): KEQY 109 m² service_junction∩service_junction full-containment.

KNOWN-OPEN — SLICE-PARTITION wedges/overlaps (part 30g/30j; the curve-native
global slice owns junction faces — cannot merge/move without breaking
elevation neutrality; the tight final weld cannot reach them without bowing
solved constrained edges).  Covers: ALL `junction~junction` /
`runway~junction` / `junction~apron` epsilon-wedges (SPJC 10, KCLT 11 incl.
runway~junction 100-163 mm, KSVH 1, KEQY junction~apron 2, HECA junction 4),
and the small `junction∩junction` overlaps (KCLT #334/335/336∩#791 [1.8/0.6/
2.3 m²], #327∩#328 [0.5], SPJC 0.2, KEXX 0.3) = task#16 KNOWN.

KNOWN-OPEN — BOUNDARY/GROUNDSIDE outline class (part 30j): the boundary
ribbon + groundside/service_road re-derive the same physical outline with
different vertex sets → sub-mm `service_road~groundside_pavement` /
`clearance~clearance` wedges (HECA service_road 2 + clearance 2, KEQY
service_road 2) and the HECA clearance∩clearance slivers (3.2 + 0.3 m²).
Longstanding structural, pre-dating 30d; the final epsilon-weld welds the
insertable seams but cannot reach solved-surface wedges.

KNOWN-OPEN — OFF-SOURCE phantom pavement (task#16): KCLT 6 large junctions
(17206 m²@21% … 2665@41%, ~lon -80.966) + 4 zero-on-source apron/junction
(496/278/135/112 m²); HECA apron #244 30 m²@0% (05R area).  Aircraft-pavement
faces the slice emitted off real source — a classification/slice question
(should be groundside, or dropped).  Rooted in the slice + pack classifier
(off-limits this session); investigate what they SHOULD be under de-seg.

KNOWN-OPEN — RUNWAY longitudinal grade: SPLP 4 findings 1.52–1.61% > 1.5%
on runway 02/20 — the same at-cap runway class as the check_grade WITHIN
SPLP=16 gate baseline (part 30i).  Emitted by the runway solver
(runway_segments/regrade/redistribute) — off-limits (de-seg session owns
runway emission).

CHECKPOINT — BUILDING∩GROUNDSIDE overlap (NOT fixed; needs coordinator
review).  KJQF building19/16 (1173+115 m²), KVUJ building8 (353 m²), HECA
building17 (6210 m²): a terminal pad wholly inside a groundside lot is
re-covered by the lot.  ROOT CAUSE (fully traced this session):
`groundside._emit_groundside_pavement_dem` DOES subtract the building union,
producing a groundside polygon with a building-shaped HOLE — but
`_dem_follow_polygon` rebuilds from `p.exterior.coords` only, dropping the
hole (verified: input holes 2 → output 0 before the fix, and 2 → 2 with a
one-line hole-carry patch).  A hole-carry patch in `_dem_follow_polygon`
ALONE is insufficient: `conformance.py` rebuilds `s.polygon = Polygon(
new_ring)` (3 sites, exterior-only) on every weld/planarize, stripping the
hole again on any groundside shape it touches (final #176 ended holes=0,
area grown).  The OSM emit is exterior-only BY DESIGN (layout.to_osm drops
all interiors for the X-Plane patch parser — same as holed junction rings,
where the punching rect's tags prevail), so the honest fix is to make the
geometry model hole-aware THROUGH conformance so check_self_overlap sees the
subtracted hole (the emit is already correct — the pad's own way covers the
hole).  That is a coordinated change to a broadly-shared pass (conformance,
touches airside too) across >2 files → tripped the CHECKPOINT gate; STOPPED
per directive.  PLAN for review: (a) `_dem_follow_polygon` carries `p.
interiors` onto the rebuilt polygon (1-line, done+reverted, safe); (b) the
three `conformance.py` `Polygon(new_ring)` rebuilds preserve `shape.polygon.
interiors`; (c) gate: wedge_audit no growth (decomposition-free, so low
risk), verify building∩groundside → 0 at KJQF/KVUJ/HECA, full suite 13.
A decomposition alternative (split holed lot into hole-free pieces at emit)
was REJECTED — the thin bridges around interior pads are prime epsilon-wedge
/ sliver generators = the exact 30j mesh-explosion class.

CHECKPOINT — APRON/JUNCTION∩SERVICE_JUNCTION overlap (NOT fixed).  SPJC
apron#65∩service_junction#72 (9.4 m², eroded-0.25 m still 5.1 → mesh-scale),
KCLT apron∩service_junction (4.3), KJQF junction∩service_junction (1.1).
ROOT (traced): `groundside.consolidate_full_width_service_corridors`
introduces it (0.0 → 9.4 m² immediately after that pass; conformance/slice do
NOT) — it absorbs+unions junction/service slivers into the merged corridor
and re-emits as service without subtracting the corridor back out of the
overlapping apron/junction.  A service-vs-airside clip (extend
`_deconflict_service_overlaps` to the service∩aircraft pair, clipping the
DEM-graded service side like `_separate_groundside_from_airside` clips
groundside) is the fix, but it perturbs the solved corridor extent/grade on
SPJC + KCLT (both FULL-suite fixtures; SPJC already carries the pre-existing
`test_no_self_overlap[SPJC]` failure that flags exactly this) → deferred for
review rather than risk the fixture set.

KNOWN-OPEN — GROUNDSIDE∩BOUNDARY overlap (task#16): KEXX groundside#17 ∩
boundary_dem_bridge#350 (1215 m²), KCLT groundside∩airport_boundary
(2.3/1.9), KVUJ apron∩airport_boundary (0.2), KSVH groundside∩groundside
(0.2).  The boundary ribbon traces OVER everything by design
(check_self_overlap's `_COVERAGE_FEATURE_ROLES` note) — the boundary/bridge
is a feature overlay, not double pavement; the large KEXX case is a
boundary_dem_bridge that co-locates with the groundside it bridges to.  Same
exterior-only-emit tolerance as the junction-hole class; benign in-sim
(overlay ribbon).  Left as KNOWN pending the same hole-aware-emit work.

## GATES (all at the committed fix)
* fast_suite: exactly the 8 pre-existing failures (2 SPLP compare + SPLP
  grade×2 + CYXY grade + CYXY terrain + CYXY route-reach + CYXY single-graph).
* FULL suite: exactly the 13 pre-existing (SPLP×4, SPJC×4, CYXY×4, HECA×1).
  No new failures; `test_no_self_overlap[SPJC]` remains pre-existing (= the
  KNOWN apron∩service class above).
* check_grade: CYXY WITHIN 1, SKIRT/PLANE/CROSS 0 (== gate); SPLP WITHIN 16.
* wedge_audit CYXY 2 (no growth).  conformance CYXY 35/1267 ramp 25 (== gate).
* Every airport re-verified post-commit: byte-identical finding counts to
  baseline except KEQY (overlap 1 → 0).

## OPEN (part 30l follow-ups)
* The two CHECKPOINT classes above (building∩groundside via hole-aware emit;
  apron∩service via service∩airside clip) — both need coordinator sign-off
  because the correct fix touches a shared pass / a full-suite fixture's
  solved geometry.
* OFF-SOURCE phantom pavement + the slice-partition wedges are de-seg's to
  clear (fewer/larger junction faces re-solved on the coarser partition).

# STATUS — SESSION 20260707 (part 30k): CLEARANCE-EFFECTIVENESS
# regression — the part-30f outer-edge DEM lift un-cut the cuts;
# REVERTED (FIX B/C kept) + new conformance PROPERTY gate
# (tools/clearance_conformance_audit.py)

USER REPORTS (in-sim, fresh bake at HEAD; HECA):
 1. MOST clearance shapes ineffective — "just going to DEM".
 2. 05R end: a clearance shape MERGED with what should be the RESA.
 3. Right-side clearance tapers into the BLAST-PAD corner, not the
    RESA corner → un-cut cliff beside the runway.
 4. Little triangle cuts in the clearance edge at runway SEGMENT nodes.

## THE ESCAPE LESSON → NEW PROPERTY GATE
The spike audit measures UNCOVERED terrain — a cut riding the DEM reads
"covered" while protecting nothing, so the 30f regression was invisible
to it (the lift even IMPROVED that number).  NEW
``tools/clearance_conformance_audit.py``: for every lateral
clearance-cut vertex, ``excess = alt − min(ceiling, DEM)``, ceiling =
nearest airside pavement edge + 1 m threshold; excess > 0.5 m =
DEM-RIDING (ineffective).  Two numbers: FLAT count (primary per-airport
A/B — includes a bounded set of lawful RESA-ramp rows) and RAMP-ALLOWED
count (above even ceiling + 5 %·d = unconditionally ineffective).

## ROOT CAUSE (empirical A/B: HECA built at 787cb6a / f2bf4f3 / 773dcb9)
DEM-riding 40/2232 (mean −0.88 m) → 508/2281 (+0.56 m) → 508 identical.
The WHOLE regression is f2bf4f3 (30f FIX A outer-edge lift); no later
commit touched cut-surface altitudes (confirms the wedge-investigation
note that the 30e/30f→HEAD diff left the outline path alone).
Mechanism: ``off = last + step`` is one station past the LAST
OBSTRUCTION, not the true daylight point, so ``DEM(off) > ceiling``
fires broadly (256 clusters airport-wide, not just sunk corridors),
tilting each strip's ruled surface from pavement (inner) to DEM (outer)
→ caps nothing.  CYXY was WORSE: 509/1337 = 38 % (ramp-allowed 480);
SPLP 24/95.
* A trapped-station-only lift (fire only when terrain never daylights
  within the band cap) MEASURED INSUFFICIENT: 497/2275 still riding,
  ramp-allowed 339 — HECA is broadly dug-in, so nearly every obstructed
  station is obstructed at the cap itself.  (Stage-1 plan corrected
  mid-flight on this measurement.)

## FIX — revert FIX A (outer row back on the ceiling); FIX B/C KEPT
The user's own item 3 settles the wall-vs-yield design tension: an
UN-CUT cliff is the complaint — the excavation, with its cut face at
the band edge, is wanted.  And the four 30f in-sim spots stay fixed
WITHOUT the lift (probes below): the needle declaw (FIX B), tighter
standoff (FIX C) and the run-taper/MultiPolygon fixes were what
actually cleaned them.  ``_build_graded_strips`` outer row is back to
``ceiling(off)`` unconditionally (pre-30e semantics, comment records
the 30k measurements).

## VERIFIED (gates; HECA/CYXY/SPLP rebuilt at the fix)
* CONFORMANCE (primary): HECA **39/2149** (mean −0.87, ramp 3) ≤
  pre-30e 40/2232 (−0.88, ramp 4) ✓.  CYXY 509 → **35**/1267 (ramp
  480 → 25) ✓.  SPLP 24 → **0**/116 (ramp 13 → 0) ✓.  HECA's residual
  39 ≈ the lawful 5 % RESA ramp rows (e.g. +13.74 m at
  30.094494,31.416804 = 0.05 × 275 m exactly).
* 30f spots STAY FIXED: 3 HECA coords inside cuts, 0 needles (3 m thr,
  40 m radius), worst ring-edge grade 3 %/3 %/2 % (pre-fix HEAD was
  4 %/4 %/16 %); CYXY notch inside cut, 0 needles, 2 % (pre-fix 3 %) ✓.
* spike audit HECA **49/38** vs the ≤48/37 gate: net +1 borderline
  sample — exact flip set identified (4 new / 3 gone, all inside the
  two KNOWN sunk-corridor partial-coverage zones 30.1164,31.4101 and
  30.1022–34,31.3946–58; 3 of the 4 new sit ≤0.64 m from an emitted
  surface = the documented mesh-constrained pavement-gap crack band,
  the 4th at 2.98 m in a zone that carried a +3.7 m sample at HEAD).
  Cause: flat outer rows decimate differently → outline jitter, not a
  new exposure class.  CYXY 224/61 (pre-fix 221/58; pre-30e 487/122 —
  the FIX C gain retained), SPLP 18/6 (pre-fix 46/11 — improved).
* check_grade: WITHIN SPLP **16** / CYXY **1** / HECA **0**; PLANE 0,
  CROSS 0, RUNWAY-END SKIRT 0 everywhere; HECA vertex-to-edge 3 +
  mid-edge 14, break 5891 — all == baselines ✓.
* wedge_audit: HECA 5, CYXY 2, SPLP 0 == baselines (no growth) ✓.
* verify_and_log HECA: identical finding CLASSES pre/post (overlap /
  off-source / epsilon_wedge; wedge 9 == 9, source 1 == 1).  Overlap
  4 → 5: one NEW 0.3 m² clearance∩clearance sliver at 30.11510,31.41375
  — the same pre-existing class as the 3.2 m² clearance∩clearance at
  30.09830,31.41876 (present both sides), outline-jitter scale ✓.
* fast_suite: exactly the 8 pre-existing failures.  Full suite: exactly
  the 13 pre-existing ✓.

## ITEMS 2/3 (05R end) — measurables resolved by item 1; cosmetics handed off
At the rebuilt HEAD the 05R box (30.093–30.102, 31.414–31.424) has ZERO
uncovered obstructing spike samples; the runway_clearance region
(-10775, 32 k m², 44 m from the 05R threshold 30.09716,31.41907)
excavates properly (airport-wide ramp-allowed = 3, none at 05R).  The
"un-cut cliff" WAS the DEM-riding cut — covered but not cutting.
Residual (cosmetic): the right-flank taper anchors at the blast-pad
corner (apt.dat 05R blast pad 65 m) rather than the RESA corner — the
ownership boundary between the 30e skirt flank-wrap, Pass A3 (which
skips END-normal stations, ``_RING_END_NORMAL_DOT``) and Pass C.  No
measurable uncovered or unconformant terrain remains there, so
reshaping that boundary is runway-end ownership work — deferred to the
de-seg session (it rebuilds runway ends; revisit on a fresh bake after
its Phase 2).

## ITEM 4 — subsumed by de-seg (documented, not fixed here)
The little triangle cuts at runway segment nodes are the per-sub-rect
Pass A3 walks: each segment's flank run ends (and run-tapers) at the
sub-rect seam, so adjacent same-ref strips meet in unmerged tapers.  A
tactical fix needs a per-ref merged-outline walk with cross-piece
altitude resampling — not cheap; the de-seg plan
(docs/runway_single_polygon_plan.md, dedicated session) removes the
seams themselves.

## OPEN (part 30k follow-ups)
* The conformance audit's FLAT count includes the lawful RESA ramp rows
  (HECA 36-39 of its baseline flags); if per-regime attribution ever
  matters, tag RESA-regime strips at emit so the audit can split them.
* Deep sunk-pavement corridors (HECA 30.115–30.116 etc.) again render a
  cut face at the band edge — by design (the cut working).  If the user
  ever rules the face too harsh THERE, the answer is a bench+backslope
  (protected band at ceiling to the cap, then a separate abutting
  backslope band to daylight) — needs multi-row emission machinery
  (today's finalize unions everything into two-row rings), NOT a return
  of the outer-row lift.

# STATUS — SESSION 20260707 (part 30i): RUNWAY SEGMENT-DIP HOTFIX —
# crown the interior cross-edges (de-seg plan Phase 0, docs/
# runway_single_polygon_plan.md).  User: "airports unusable as is".
#
# THE DEFECT: a crowned runway emits as abutting sub-rects; every interior
# segment CROSS-EDGE is a constrained mesh edge whose ONLY nodes are the two
# corner vertices — both carrying the full crown drop (profile − rate·hw).
# The edge cuts FLAT ACROSS at the dropped altitude while the surface between
# segments carries the centerline ridge (crown_spine at profile) → the mesh
# dives from ridge to cross-edge and back at EVERY segment line = a visible
# centre DIP on every crowned runway.  Probe (CYXY base): 24 flat full-width
# interior cross-edges, 0 crowned.
#
# THE FIX (Phase 0 — does NOT de-segment): insert a CENTERLINE node at each
# interior cross-edge's axis intersection at the runway PROFILE altitude (crown
# drop 0 on the axis), into the rings of BOTH abutting sub-rects at the
# IDENTICAL midpoint so the emit consensus WELDS them into one node (a one-
# sided insert would mint a T-vertex/tear).  Each cross-section becomes a tent
# (corner-low → centre-high → corner-low) matching the crown.  Centre altitude
# = the persisted profile (runway_redistribute._interp_profile at the station)
# — the SAME source the crown_spine breakline uses, so the two constraints
# agree (no duplicate near-coincident constraint = the wedge class).
#
# ## LANDED (part 30i)
# 1. crown.insert_runway_crossedge_crown_nodes(layout) — the whole fix; called
#    as the ABSOLUTE-LAST geometry touch (pipeline, beside the probe-node hook,
#    after decimation / final projection / skirts; a mid-edge tent vertex is
#    the 3D-collinear class emit decimation removes, so it must arrive last).
#    Groups ROLE_RUNWAY sub-rects by ref; a canonical-edge shared by exactly 2
#    distinct sub-rects = an interior cross-edge (long / end edges belong to one
#    sub-rect, never shared → skipped by construction — ends read by skirts/
#    RESA are untouched).  Skips seam-band cross-edges (tile-seam pins are
#    cross-tile terrain contracts; tile_cut._SEAM_LINE_TOL_M).  Runway↔crossing
#    edges have <2 ROLE_RUNWAY owners → skipped (no one-sided insert; the
#    crossing dome already puts drop 0 on the axis).  Gate O4_RUNWAY_XEDGE_CROWN
#    (default 1); inherits ENABLE_SPINE_CROWN + CROWN_RUNWAYS.
# 2. VALIDATOR: the centerline nodes are exported to the axes sidecar
#    (layout.to_osm → "crown_centerline") and check_grade skips runway within-
#    shape all-pairs plane pairs that touch one (_crown_centerline_nids) —
#    exactly the crown_spine-breakline exemption class: a cross-station diagonal
#    to a ridge node conflates the LONGITUDINAL profile (the SPINE PROFILE
#    check's domain) with the sub-cap LATERAL crown.  Without this the extra
#    centerline samples on SPLP's at-cap runway tripped within 16→31 (same
#    marginal 1.6% class, just more pairs).  check_grade + verification.py +
#    tests/test_pavement_grade.py all thread the new field.
# 3. verification.check_runway_profile: reconstruct cross-ends from the runway
#    AXIS (cluster corners at the two extreme stations, take the EDGE elevation
#    = MIN of the cluster so the inserted ridge node is excluded) so a crowned
#    5+-corner sub-rect's longitudinal profile is still measured — else the
#    old ``len==4`` gate SKIPPED crowned rects and MASKED SPLP's real >1.5%
#    profile (test_runway_longitudinal_grade[SPLP] flipped to a false PASS).
#    Behaviour is byte-identical for uncrowned 4-corner rects (MIN==AVG at each
#    flat cross-end).
#
# ## VERIFIED (gates, part 30i — all at 136c6a0 baselines)
# * Cross-section probe (emitted OSM, sidecar-identified centerline nodes):
#   CYXY 24 tents (+0.12/+0.15/+0.23 m = the per-ref crown drops exactly),
#   SPLP 8 (+0.23), HECA 53 (+0.30), KCLT 4.  Baseline: 0 crowned, all flat.
#   Every crowned segment centre lifts by the crown drop above its edge corners.
# * check_grade: SPLP within 16 (== baseline), CYXY 1, HECA 0, KCLT 6; plane 0,
#   cross 0 everywhere; HECA vertex-to-edge 3 + mid-edge 14, break 5891 (the
#   quarantined-by-design class); SPINE PROFILE + skirt sections unchanged.
# * wedge_audit (uncommitted; NOT committed by this task): ZERO new wedges —
#   CYXY 2→2, SPLP 0→0, HECA 5→5.  No tear, no near-coincident duplicate.
# * O4_SPINE_CROWN=0 / O4_RUNWAY_XEDGE_CROWN=0: 0 cross-edges crowned (gated).
# * fast_suite: exactly the 8 pre-existing failures (identical set — the fix
#   RESOLVED none by masking; the verification.py cross-end fix keeps
#   test_runway_longitudinal_grade[SPLP] correctly RED).  Full suite: exactly
#   the 13 pre-existing failures.  Zero new.
# * Conformance invariant (SPLP 1 residual T-junction, HECA 13/3): IDENTICAL
#   base vs fix — pre-existing (the crown runs after conformance), not worsened.
#
# ## OPEN (part 30i follow-ups)
# * Runway↔runway_crossing interior cross-edges are NOT centre-crowned (only
#   ROLE_RUNWAY↔ROLE_RUNWAY pairs are).  The crossing dome already puts drop 0
#   on the axis, so a crossing-abutting cross-edge dips less; a full fix waits
#   for de-seg Phase 2 (the crossing becomes one welded ring).
# * The hotfix keeps segments; de-seg (Phases 1–3, docs/runway_single_polygon_
#   plan.md) still removes the interior cross-edges entirely.  This de-risks the
#   Monday deadline: the dips die now.

# STATUS — SESSION 20260707 (part 30j): KJQF EPSILON-WEDGE triangle
# explosion — final weld on the boundary↔groundside seam
# (1,993,832 → 14,252 isolated tris; +to_osm zero-edge guard + verify tripwire)

## LANDED (part 30j) — final epsilon-wedge weld + zero-length-edge guard
## + always-on wedge detector in the verify pass

THE DEFECT.  KJQF's fresh patch costs ~2.0M triangles (~55 % of tile
+35-081) via EPSILON WEDGES: two constrained edges share a node, run
near-parallel (< 0.01°), and diverge by sub-millimetre.  Triangle4XP's
Ruppert encroachment rule ping-pongs edge splits on the near-zero-area
sliver down to machine epsilon, exploding the tile.  MEASURED source:
the ``boundary`` ribbon and the ``groundside_pavement`` lots RE-DERIVE
the same physical outline with DIFFERENT vertex sets — a groundside lot
edge that runs ALONG the ribbon inner edge ends up with a ribbon vertex
sitting ON it (perp 0.0001–0.5 mm, projecting mid-edge) WITHOUT a shared
node.  21 such pairs at KJQF (way -10586 boundary ↔ -10177 groundside,
shared node -2183; boundary vertex -3574 sits 8.5 m along the 15.06 m
groundside edge -2183→-2184 at 0.00012 mm perpendicular).

NOT THE 30e/30f REGRESSION IT WAS FRAMED AS.  Measured control: KJQF at
787cb6a (pre-30e) = 26 wedges / 21 boundary~groundside, IDENTICAL to dev
136c6a0.  The 30e/30f diff (``boundary.py`` bridge↔skirt reconcile,
``clearance.py`` outer-edge lift + needle declaw) does NOT touch the
boundary/groundside OUTLINE path — the wedge class is a LONGSTANDING
structural issue, pre-dating 30d.  MECHANISM: the final T-vertex weld
``enforce_conformance(tol=0.01, include_overlay_refs=True)`` runs at
pipeline.py:5534, but THREE geometry-mutating passes run AFTER it —
``_separate_groundside_from_airside`` (rebuilds lot rings by re-sampling
the DEM-follow outline), ``decimate_emit_nodes`` (drops per-shape ring
vertices independently), and the runway-end skirts.  Those RE-DERIVE a
neighbour's outline with a fresh vertex set, DE-CONFORMING the seam the
5534 weld just welded.  (Decimation OFF makes KJQF WORSE — 53 wedges —
so decimation is not the cause; the independent outline re-sampling is.)

THE FIX (three parts).
 1. **Final epsilon-wedge weld** (``pipeline.py``, after the skirt emit /
    reconcile, before the O4_PROBE_NODES insert): re-run
    ``enforce_conformance(tol=0.01, include_overlay_refs=True)`` on the
    FINAL vertex sets so each on-edge foreign vertex is inserted into the
    edge it lies on → shared node → the sliver vanishes.  TIGHT 0.01 m
    tolerance (the wedge class sits at 0.000–0.003 m perp); a wider tol
    would bow edges / mint hairline overlaps.  Insert-only at interpolated
    altitudes (surface-neutral), safe as the last production geometry
    touch.  KJQF: inserts 11 vertices, kills all 21 boundary~groundside
    wedges.  No-op where there is no such seam (HECA 0, SPLP 0, CYXY 0).
 2. **Zero-length-edge guard** (``layout.py`` ``_ring_to_nids``): two ring
    vertices at the SAME canonical XY but Δalt > VERTEX_ALT_MERGE_TOL_M
    get DIFFERENT node ids (a wall/cliff) — but a wall cannot have zero
    horizontal extent, so a 0.00 m constrained edge results (KJQF
    taxiway_clearance -3870→-3871, Δalt 4.2 m).  Collapse consecutive
    (and the wrap-around closing) same-coordinate nids to the first-kept
    vertex.  KJQF near-zero segments 1 → 0.
 3. **Wedge detector in the verify pass** (``verification.py``
    ``check_epsilon_wedges`` + wired into ``verify_and_log`` counts +
    debug lines as ``epsilon_wedge`` / ``EPSILON-WEDGE``): the always-on
    regression tripwire so a future emitter that mints a fresh unwelded
    outline is flagged per-airport.  Mirrors ``tools/wedge_audit.py``
    (committed, now CLI-capable: ``wedge_audit.py file.osm [--lat N]``).

## VERIFIED (gates, part 30j)
* WEDGE AUDIT (< 0.5°, < 20 cm, > 0), fresh builds in this worktree —
  boundary~groundside class ELIMINATED everywhere:
    KJQF  26 → 5   (was 21 boundary~groundside + 5 junction; now 5 junction)
    KCLT  15 → 12  (junction~runway/junction — a DIFFERENT emitter)
    HECA   5 → 5   (no boundary~groundside; unchanged, fix is a no-op)
    SPLP   0 → 0
    CYXY   2 → 2   (junction~runway 104 mm/0.11° — real geometry, unchanged)
  The residual junction/​runway wedges are NOT the boundary/groundside
  regression: they are bound to the solved slice-partition geometry (the
  part-30g negative result — junction faces cannot be merged/moved without
  breaking elevation neutrality) and the tight weld cannot reach them
  without bowing solved constrained edges.  DOCUMENTED, not fixed, per
  this task's "fix if weld-reuse covers them, else document" scope.
* ISOLATED TRIANGULATION (/tmp/meshdiag, frame + patch + real tile .alt):
    KJQF  1,993,832 → 14,252 tris   (gate < 15K — MET; −99.3 %)
    KCLT    145,260 → 130,614 tris  (gate ≤ 200K — MET)
    HECA     44,810 → 44,810 tris   (gate ≈ 40K — byte-identical, no-op)
* check_grade at gate baselines: WITHIN-SHAPE SPLP **16**, CYXY **1**,
  HECA **0**; RUNWAY-END SKIRT edge / PLANE GRADIENT / CROSS-SHAPE all
  **0** everywhere; HECA steps **3 + 14** (unchanged).  (KJQF WITHIN
  8 → 11: the 11 newly-welded T-vertices add a few sub-2 % within-shape
  pairs — KJQF is not a check_grade gate airport; SPLP/CYXY/HECA gates
  hold exactly.)
* HECA clearance_spike_audit **48 samples / 37 clusters** (≤ 48/37 gate —
  the 30f fixes stay fixed).
* tools/fast_suite.sh: exactly the **8** pre-existing failures
  (SPLP compare×2 + grade×2, CYXY×3, SPLP/CYXY grade — identical IDs).
  Full suite: exactly the **13** pre-existing (SPLP×4, SPJC×4, CYXY×4,
  HECA×1) — no new failures.  test_layout + test_verification_checks: 35
  pass (the to_osm guard + verify wiring green).

## OPEN (part 30j follow-ups)
* Residual junction~junction / junction~runway wedges (KJQF 5, KCLT 12,
  HECA 3–5) — a SEPARATE emitter (the curve-native global slice partition,
  part 30g).  They do NOT explode the mesh at KCLT (130K, under gate) —
  the boundary/groundside class was the only ~2M driver — but a
  slice-partition fix (fewer/larger junction faces, per-face profiles
  re-solved on the coarser partition) would clear them.  Out of this
  task's "don't touch the solver/slice" scope; tracked as 30g's
  recommended follow-up.
* The final weld is the LAST production geometry touch.  O4_PROBE_NODES
  (opt-in, normally unset) inserts vertices AFTER it and would re-introduce
  on-edge nodes — acceptable, since probes are a diagnostic-only path.


# STATUS — SESSION 20260707 (part 30f): in-sim CLEARANCE defect fixes —
# sunk-pavement outer-edge WALL + resample NEEDLES + tighter standoff
# (HECA/CYXY in-sim eval: terrain spikes at jogs, pointy cuts, deep notch)

## LANDED (part 30f) — clearance cuts hug pavement + daylight as a
## backslope instead of walling off; single-vertex spikes clamped
USER REPORTS (in-sim, HECA "looks great" otherwise; CYXY):
 1+2. HECA 30.1165887,31.4109619 / 30.1166179,31.4111917 — terrain
    spikes at little jogs in clearance shapes.
 3.   HECA 30.0974775,31.4075072 — three POINTY cuts misaligned with the
    pavement they follow.
 4.   CYXY 60.7121148,-135.0702708 — a strange DEEP NOTCH in a clearance
    shape.
 5.   Cuts should hug the pavement — reduce the margin more.
 6.   CYXY 60.7092306,-135.0738928 — service-road spine "big ridge"
    (DIAGNOSE ONLY).

ROOT CAUSE (one class for 1–4).  The Pass A3 flat-shadow cut cuts terrain
above the pavement-edge ceiling down to it and DAYLIGHTS where the DEM
drops back to the ceiling.  But where the pavement is SUNK in a plateau
(HECA apron/service-road corridors excavated ~14 m below grade; the CYXY
14R/32L NW threshold at 694 m in 715 m terrain) the terrain NEVER drops
back to the ceiling within the band cap.  The outer daylight edge then
planted a FLAT shelf at pavement level under 14 m of standing terrain — a
vertical WALL at the band edge (items 1/2/4, the "spike at a jog" / "deep
notch") and a submerged shelf whose corners splay past the pavement at
convex jogs (item 3, the "pointy cuts").

FIX A — outer-edge lift-only (``_build_graded_strips``): the outer
(daylight) row rides the HIGHER of the ceiling and the DEM — the exact
mirror of the skirt's ``_skirt_lift_alt`` convention.  Where terrain has
daylit this is the ceiling (unchanged); where it has not, the outer edge
rides UP to meet the standing terrain as a cut BACKSLOPE instead of a
wall.  Only ever RAISES the outer edge → never carves a sub-surface
canyon (the CLEARANCE_LATERAL_MAX_SLOPE=0 canyon guard is preserved).

FIX B — needle declaw (``_declaw_alt_needles``, ``_NEEDLE_ALT_TOL_M`` =
3 m).  Fix A makes the inner edge ride pavement level and the outer edge
ride terrain, so at a concave jog of a thin sunk-pavement corridor the
inner and outer SOURCE strip edges pass within the resampler's
``EDGE_TOL_M`` (0.5 m); one final-ring vertex flips to the far edge and
spikes ~7 m above/below its neighbours (a single-vertex needle — the
residual in-sim spike).  ``_finalize`` clamps any vertex differing from
BOTH ring neighbours by > 3 m (neighbours agreeing to within 3 m) to the
neighbour mean — the LAST altitude op before emit + the ``adopt`` seam
store, so a spike from resample, sibling adoption OR the coincident-vertex
merge is removed and never propagates.  (HECA: fix A alone introduced 6
needles; declaw → 0.  CYXY had 3 needles at BASELINE → 0.)

FIX C — item 5, tighter standoff: ``_PAVEMENT_GAP_M`` 1.5 → 1.0 m,
``_RING_PROBE_M`` 2.0 → 1.5 m.  Still 2× the 0.5 m merge / edge-proximity
floor (``SHARED_VERTEX_TOL_M`` = ``check_vertex_on_sloping_edge``
``EDGE_PROX_M`` = 0.5), so the inner edge never lands on or merges with
pavement.  Verified: HECA min cut-vertex-to-pavement 0.751 m, 0 vertices
within 0.5 m; CROSS-SHAPE (≤0.5 m) grade = 0 at all airports.

## VERIFIED (gates, part 30f — all at f9d5103 baselines)
* ``clearance_spike_audit`` HECA **205 → 48 samples / 37 clusters** (well
  under the 203/119 gate — the tighter standoff halves the deliberate
  pavement-gap crack band).  CYXY **487/122 → 220/57**.  SPLP 19/7.  The
  three reported HECA spots + the CYXY notch are covered (nearest residual
  6.4 m from HECA coord-1, a 2-sample crack beside a service road sunk
  6.5 m — mesh-constrained, inherent to sunk pavement).
* Per-site: CYXY notch max cut ring-edge grade **38 % (1.4 m/3.7 m wall)
  → 2 % (0.9 m/58.8 m smooth backslope)**; notch cut alt range 694–708 →
  694–716 (rides up to terrain).  HECA coord-1 needle 6.9 m (fix-A only)
  → 0.  All 4 coords inside clean cuts, 0 needles (thr 3 m) across HECA +
  CYXY.
* check_grade at f9d5103 baselines: WITHIN SPLP **16**, CYXY **1**, HECA
  **0**; PLANE 0, CROSS 0, RUNWAY-END SKIRT edge 0 everywhere; HECA steps
  3 + 14 (unchanged).
* fast_suite.sh: exactly the 8 pre-existing failures (identical test IDs
  to a stashed f9d5103 run).  Full suite: exactly the 13 pre-existing
  (SPLP×4, SPJC×4, CYXY×4, HECA×1) — no new failures.

## ITEM 6 VERDICT (DIAGNOSE ONLY) — STALE-BAKE ARTIFACT
CYXY service-road spine "big ridge" at 60.7092306,-135.0738928: HEAD
emits NOTHING elevated there.  ``CROWN_SERVICE`` defaults OFF (config.py:
runway-only crown scoping since 1ed5cc6); all 8 ``crown_spine`` breaklines
in the HEAD CYXY patch are RUNWAY spines (z 693–706, nearest 481 m from
the site).  The service_road (-10204) at the site follows DEM (res ≈ 0,
no transverse ridge); the service_junction (-10065) 4 m spread is
LONGITUDINAL (road descends 709→705 along its length).  The user's tile
predates the crown scoping — a RE-BAKE clears the ridge.

## OPEN (part 30f follow-ups)
* The sunk-pavement corridors (HECA 30.115–30.116, service roads/apron
  ~14 m below a plateau; CYXY 14R/32L NW threshold) still carry a
  mesh-constrained crack-band residual (a few 1–2-sample audit clusters)
  where the pavement edge itself abuts 6–14 m of standing terrain — the
  cut inner edge MUST sit at pavement level for wingtip protection, so the
  step at the pavement/cut boundary is inherent, not a cut defect.  If it
  ever needs closing, the pavement solver (not the clearance sweep) is the
  place — the pavement is genuinely dug in.
* Reducing ``_PAVEMENT_GAP_M`` below 1.0 m would approach the 0.5 m merge
  floor; 1.0 is the practical minimum with the current tolerances.



# STATUS — SESSION 20260707 (part 30g): KCLT junction mesh-density — the
# fix does NOT live emit-side; the triangles are bound to the solved
# constrained-edge geometry (measured, negative result)

## VERDICT
KCLT's junction triangle load resists every EMIT-SIDE reduction that
respects the elevation / law / conformance invariants.  The target
(full patch 144K → <80K, junction class ~125K → <60K, HECA held at 40K)
is NOT reachable by ring decimation or parallel-sliver merge as scoped.
The triangles are inextricably bound to the constrained-edge geometry
that ENCODES the solved elevation field: you cannot remove the tris
without removing constrained edges, and removing them either changes the
surface (breaks elevation neutrality) or breaks the conformance invariant
the pipeline maintains at a delicate equilibrium.  No behavior-changing
code was committed — this entry is the durable finding.

## MECHANISM (isolated-triangulation harness, /tmp/meshdiag, f9d5103)
Baselines (frame-box + patch through Triangle4XP with the tile's CLI
params, real +35-081 / +30+031 .alt):

    KCLT full 144,264 · no-junction 19,522 · JUNCTION CLASS 124,742
    HECA full  40,098 · no-junction 23,420 · JUNCTION CLASS  16,678

KCLT junctions IN COMPLETE ISOLATION (frame + junction ways only) cost
47,070 tris (639 ways); HECA 20,948 (400 ways).  So of the 125K
"junction-class" cost, only ~47K is the junctions' own triangulation —
the other ~78K is refinement junctions INDUCE in the neighbours they
squeeze against (apron / *_clearance / crown_spine).  Proof: dropping any
ONE neighbour class alone barely moves the count (all ≈140K), but
dropping junction + all its neighbours → 12,228.  It is a COUPLED
constrained-edge system; the interfaces are the cost.

WHY KCLT ≫ HECA: fragmentation + packing, not node count.  KCLT has 639
junction faces in 1.6M m² (median 586 m²); HECA 400 faces in 2.5M m²
(median 496).  KCLT packs 60% MORE faces into 36% LESS area, so its
shared-boundary edges are shorter and denser (1397 shared-edge pairs vs
HECA 899).  Triangle4XP's -q10 (10° min-angle) quality mesh refines every
thin region between densely-interleaved constrained edges into needle
triangles.  Top hotspot: one 55 m cell holds 14,709 tris where junction
faces -10339/-10340 (542×79 m and 305×79 m, 55-59 nodes) run 2.8 m apart
with a 30 m² filler sliver (-10719) wedged between; a second cell holds
9,521 where two junctions sit 2.3 m apart.

DEFINITIVE lever check: dissolving the 639 junction faces into their 14
connected-component UNIONS (removing internal shared boundaries) drops
junction-only cost 47,070 → 6,912 (nodes 7,967 → 1,339).  The internal
constrained edges ARE the driver.  But those edges carry the per-face
solved profiles — dissolving merges floors (violates elevation
neutrality) and, in the full patch, the dissolved rings pinch against the
un-merged neighbours → 3.16M tris (degenerate).

## LEVERS MEASURED (all fall short or are unsafe)
- Decimation Z band 0.02→0.15 m (global): 143,924 → 126,804 (−17K).  Far
  short of 80K, and 0.15 m reintroduces the V15 waviness the 0.02 m band
  was tuned to suppress.  Junction-SCOPED would yield even less.
- Densification OFF (O4_DENSIFY_JUNCTION_EDGES=0): 143,924 → 142,388
  (−1.5K).  Densification is NOT the driver.  Short edges (<12 m) already
  get zero densification (densify_junction_edges k=0).
- Coarser spine step (O4_JCT_SPINE_STEP_M=24): 183,172 — WORSE.  Fewer
  edge nodes = bigger gaps for the interior refinement to fill.
- apt-zone density WEIGHT off (harness weight_on=False): 140,140 (−4K).
  The ×4 apt weight is not the driver; the constrained geometry is.
- Elongated-sliver removal (77 faces, area<300, aspect≥3): 143,924 →
  128,962 (−15K) but DESTRUCTIVE (uncovers pavement) — not a real fix.
- Sliver-merge with SPINE VETO OFF (O4_SLIVER_SPINE_VETO=0): build HUNG
  (killed after 8 min; normal build 140 s).  The veto is load-bearing:
  unvetoed unions produce degenerate geometry a downstream pass thrashes
  on.  With the veto ON, KCLT merges 0 (all 179 candidates spine-vetoed —
  their shared edge IS a slice-cut spine line whose nodes carry the solved
  profile; the veto guards exactly the elevation fidelity this task must
  not break).
- Post-hoc edge surgery (snap 3226 gap vertices onto foreign edges;
  buffer-union merge): both → 3.16M tris.  Any edge op done OUTSIDE the
  pipeline's weld/conformance machinery mints self-intersections /
  zero-area slivers that triangulate catastrophically.  Confirms the
  geometry sits at a conformance equilibrium.

## WHERE THE TRIANGLES RESIST (one line)
The 47K junction-own + 78K induced load lives in the shared-boundary and
near-parallel-gap edges between KCLT's densely-packed junction faces.
Collapsing them needs a SOURCE-side change — the curve-native global
slice emitting FEWER, LARGER junction faces (KCLT 639 vs HECA 400 for
less area), with per-face profiles re-solved on the coarser partition —
NOT an emit-side ring/geometry edit.  That is a solver/slice-partition
change (out of this task's "don't touch solver / re-solve elevations"
scope) and would flip compare-target floor counts.  Recommend routing the
real fix through the slice partition (pavement/global_slice.py +
junction classification), gated, with the compare-target fixtures re-cut
deliberately — tracked as a follow-up, not an emit hotfix.

## HARNESS (rebuilt/verified this session — the gate)
/tmp/meshdiag/isolate.py (+ isolate_file.py, isolate_only.py,
isolate_noweight.py) replicate include_patches() insertion + the O4 mesh
CLI against the real tile .alt.  patch_stats.py / needle2.py /
parallel.py / subseg.py / coplanar.py / hotspot.py characterise the
junction class.  ISO_LAT/ISO_LON/ISO_ALT env select the tile.

---

# STATUS — SESSION 20260707 (part 30e): runway-end SKIRT lift-only fix +
# boundary→DEM BRIDGE ↔ skirt/RESA reconciliation (KCLT 18R in-sim ramp)

## LANDED (part 30e) — skirt is FILL-only (never cuts), bridge matches skirt
USER REPORT (in-sim, KCLT 18R): a ramp carved BELOW grade at a runway
end.  Two causes, both fixed:

### A. Skirt lift-only (fill never cuts — the flat-shadow mirror)
The runway-end skirt (``clearance.emit_runway_end_skirts``) enforces a
MINIMUM grade: it FILLS terrain that falls too steeply below the law
floor (``grade_law.runway_end_skirt_floor_profile``); the RESA cut
(Pass C) separately handles terrain that RISES.  The skirt must be
FILL-ONLY, but it emitted vertex altitudes at the analytic floor
UNCONDITIONALLY — so terrain inside a triggered band that sits ABOVE the
floor (a bump in a hollow; the last+step daylight overshoot) was graded
DOWN to the floor: an unnecessary cut ramp.

FIX — per-vertex lift-only, ``skirt_alt = max(analytic_floor, DEM)``, via
ONE shared helper ``clearance._skirt_lift_alt`` at all three emit sites:
``_build_filled_skirts`` ring altitudes (inner + outer rows) AND the two
analytic ``alt_at`` closures (``_end_alt_at``, ``_flank_alt_at``, now
closing over ``sample_dem``) used when the finalize clip recomputes
vertices.  Mirrors the cut passes' flat-shadow convention (cuts never
fill; fills never cut — docs/STANDARDS.md "Lateral (wingtip) clearance").
Shared band-boundary rows compute the SAME max'ed value at shared
vertices (identical position + DEM sample + rounding) → no surface tear.

VALIDATOR lockstep (``tools/check_grade._check_runway_end_skirt_edges``):
the DEM-free edge-grade reader assumed level band rows and flagged any
edge steeper than the down-grade cap.  Lift breaks levelness — a bump
vertex descending to a floor vertex can exceed the cap LAWFULLY (the law
bounds how far BELOW the floor, not how the surface rides a bump back
up).  DEM-free we cannot read the floor, but we tell lawful-lift from
corruption by SHAPE: a lift RAISES a vertex above its ring neighbours (a
peak); post-emit corruption DROPS one below them (a valley).  The reader
now skips an over-steep edge whose HIGHER endpoint is a local peak (a
lifted DEM bump) and still flags genuine over-steep descents.  The
DEM-aware ``verification.check_runway_end_skirt`` remains the full
below-floor law check (unaffected by lift — lifting only RAISES the
surface, which can never read as a below-floor drop).

NOTE (measured): at SPLP (6 skirts) and KCLT (48 skirts, 4392 emit-time
skirt vertices) NO skirt vertex has DEM above the analytic floor, so the
lift is a no-op there (before == after == 0 down-cutting vertices).  The
fix is a correctness guarantee for bump terrain; the VISIBLE 18R ramp was
cause B.

### B. Boundary→DEM bridge ↔ skirt/RESA reconciliation
The boundary→DEM bridge emits in the feature phase, BEFORE the final
grade projection and the skirts (which are the absolute-LAST emission —
they bake the floor from the settled pavement profile; the KCLT 18L
+0.4 m case in the skirt call-site comment).  So a bridge at a runway
end anchors its inner edge to RAW DEM and cannot match the skirt/RESA
surface emitted later → the two meet in a step (KCLT 18R: **10.2 m**
bridge-vs-skirt mismatch — the ramp the user saw).

FIX (option b, contained — the skirt STAYS last): new
``boundary._reconcile_boundary_bridges_with_skirts``, called right after
the skirt emit + tile-cut in ``pipeline.py``.  Per bridge it (1)
SUBTRACTS any overlapped skirt/RESA (role ``runway_clearance``) area —
the skirt owns the graded terrain in its governed zone — then (2)
RE-ANCHORS every surviving bridge vertex within 8 m of a skirt/RESA
surface to that surface's edge-interpolated altitude, so bridge and
skirt meet FLUSH.  (Bridge + skirt are separated by the skirt's
pavement-gap/clip buffer, so they ABUT rather than overlap — the
re-anchor, not the subtraction, closes the step.)  Reuses the
``_clip_boundary_bridges_against_pavement`` difference + largest-piece +
``_resample_node_altitudes_nn`` machinery.  Option (a) (move bridge emit
after skirts) rejected: wide blast radius (bridge feeds snap-to-corner,
junction-contact insertion, tile-cut, feature conformance) and it fights
the skirt-must-be-last invariant.

## VERIFIED (gates, part 30e — all at 787cb6a baselines)
* KCLT **18R** (primary probe): bridge-vs-skirt mismatch **10.20 m → 0.00
  m**; 2 bridges reconciled, all 8 bridges preserved (not destroyed).
* Down-cutting skirt vertices (DEM−alt > 1 cm): SPLP 0→0, KCLT 0→0 (see
  NOTE above — lift is a no-op at these airports; verified at emit time
  over 4392 KCLT skirt vertices, 0 above-floor).
* check_grade (gate-on builds, all fixes): WITHIN-SHAPE SPLP **16**, CYXY
  **1**, HECA **0**; "RUNWAY-END SKIRT edge grade" **0** at SPLP / CYXY /
  HECA / KCLT (checker updated, still flags genuine over-steep — 3
  reader unit tests green).
* ``verification.check_runway_end_skirt`` at KCLT: **0** findings (skirt
  law conformance preserved; the KCLT M4 baseline holds).
* fast_suite.sh: exactly the 8 pre-existing failures.
* full suite: exactly the 13 pre-existing failures (SPLP ×4, SPJC ×4,
  CYXY ×4, HECA ×1) — no new failures.

## OPEN (part 30e follow-ups)
* The lift-only fix has no observable effect at the current fixtures (no
  above-floor skirt vertices).  A synthetic bump-in-band terrain confirms
  the emitter lifts and the checker no longer false-flags (143.7 % edge
  → 0 flags), but a REAL airport with a bump inside a triggered band
  would be the true regression witness — none in the fixture set.
* Bridge re-anchor tolerance is 8 m (one skirt station step + slack).  A
  bridge vertex >8 m from any skirt edge keeps its DEM value; if a future
  airport has a bridge frontier coarser than that, widen the tol.

# STATUS — SESSION 20260707 (part 30d): TAXIWAY-EDGE grade adoption for
# service roads (USER RULING part-29 item 4) — mirrors the apron-edge rule

## LANDED (part 30d) — taxiway-edge service-road grade adoption
USER RULING (2026-07-07, durable law, STATUS part 29 item 4): like the
existing APRON-edge adoption, the PORTION of a service road that is
INSIDE or SHARES A LONG EDGE with a TAXIWAY follows the more limiting
(taxiway) grade law — 1.5 % (letter-aware) instead of the road's 5 %.
Only isolated narrow-road stretches (nothing along their long edge) keep
the full road cap.  PORTION-based: split at the band boundary, exactly
like the apron-edge rule.

MECHANISM (extended the part-28 apron-edge adoption end-to-end; same shape):
1. **Pipeline pass** (``pipeline.py``, immediately AFTER the apron-edge
   pass): taxiway band = union of the taxi family (``ROLE_JUNCTION`` +
   the 4 taxi-rect roles: primary/secondary_parallel, stub,
   cross_connector) buffered ``SERVICE_ROAD_WIDTH_M + 2 m`` (join_style=2)
   — the SAME band construction the apron rule uses.  Eligible
   ``service_road``/``service_junction`` shapes that SHARE ≥1 m of the
   taxi boundary OR OVERLAP taxi pavement (inside) are split at the band:
   inside pieces set ``adopts_taxi_grade=True`` + ``adopted_taxi_letter``
   (the nearest taxi shape's ICAO code letter); outside pieces keep the
   service law.  Wholly-inside/alongside → adopts whole.  APRON (1 %) is
   MORE limiting than taxi (1.5 %), so the pass runs after the apron pass
   and SKIPS any piece already ``adopts_apron_grade`` (apron wins).
2. **Flag** (``layout.py`` ``BuiltShape``): new ``adopts_taxi_grade`` +
   ``adopted_taxi_letter`` (parallel to ``adopts_apron_grade``; existing
   apron flag + all its consumers untouched → backward compatible).
3. **Solver caps**: ``_shape_grade`` (solver_primitives), ``_body_cap``
   (grade_graph), the sloping-rect cap path, and the GradeShape
   propagation all resolve ``adopts_taxi_grade`` →
   ``taxi_grade_cap_for_letter(adopted_taxi_letter)`` (None → 1.5 %
   ``TAXI_MAX_GRADE``).  Apron branch checked first so apron wins.
4. **Emission tag** (``layout.to_osm``): ``o4_grade_law='taxi'`` (+
   ``code_letter`` for the letter-aware cap) on adopted pieces.
5. **Validator** (``tools/check_grade.py``): ``o4_grade_law='taxi'`` →
   ``taxi_grade_cap_for_letter(code_letter)`` in ``get_grade_limit``, and
   the OSM GradeShape reader propagates the flag + letter so solver and
   validator read the SAME cap.
6. **Fragment plumbing**: ``elevation.py`` extra-fragment rebuild carries
   the new fields (mirrors the apron flag).

## VERIFIED (gates, part 30d)
* PROBE (CYXY + HECA, smoothed-DEM cached build = the test frame):
  - CYXY: 1 adopted whole + 1 split; the 1 surviving adopted piece
    (service_junction) emits ``o4_grade_law='taxi'`` and SOLVES at
    1.46 % (≤ 1.5 %).  Its 15 ISOLATED sibling road pieces still grade up
    to the full 5.00 % cap (portion split works).
  - HECA: 5 adopted whole + 10 split; 6 surviving adopted pieces (4
    service_junction + 2 service_road) all emit ``o4_grade_law='taxi'``
    and SOLVE at 1.43 / 1.37 / 1.27 / 1.00 / 0.90 / 0.68 % (all ≤ 1.5 %).
    40 isolated road pieces still grade to 5 %+ (max 13.6 % over steep
    terrain — correctly UNcapped, no long taxi edge).
* NON-REGRESSION (baseline 1ed5cc6 vs this change, same measurement):
  within CYXY 1→1, HECA 0→0, SPLP 16→16; cross/steps IDENTICAL
  (HECA cross 10, vertex-to-edge 3 + mid-edge 14).  Break-region
  growth tiny: CYXY 772→779 (+0.9 %), HECA 11067→11084 (+0.15 %) — both
  well under the +2 % watch threshold; NO new within violations on any
  adopted road.  No infeasible pocket surfaced (adopted pieces sit inside
  the already-flattened taxi solve; the apron rule's mouth/band
  exemptions were NOT needed).
* fast_suite: EXACTLY the 8 pre-existing failures, zero new.
* FULL suite: EXACTLY the 13 pre-existing failures
  (splp compare ×2, pavement_grade SPLP, runway_longitudinal SPLP,
  compare_spjc, no_self_overlap SPJC, pavement_grade SPJC,
  cyxy_taxi_e_south_apron, route_band_zero SPJC, pavement_grade CYXY,
  cyxy_route_reach, solver_validator_same_edge_budgets,
  pavement_grade HECA), zero new.

## OPEN (part 30d follow-ups)
* A standalone taxi-adopted ``service_road`` piece (not in PAVEMENT_ROLES
  nor SOFT_VISIBILITY_ROLES) has no direct solver within-shape
  constraint — like the apron rule, its 1.5 % is enforced at the
  VALIDATOR (``o4_grade_law='taxi'``) and inherited from the flattened
  taxi solve its vertices sit in.  Held at HECA (0.68-0.90 %); if a future
  airport puts an adopted road over steep terrain WITHOUT a co-solved
  taxi host it could read over-cap — same latent property the apron rule
  carries.  Would need service_road in PAVEMENT_ROLES to solve-enforce.

# STATUS — SESSION 20260707 (part 30c): CROWN runway-only scoping +
# runway-crossing drainage-dome blend + continuous crossing ridge
# (in-sim crown eval iteration; builds on part 30/30b crown v2)

## LANDED (part 30c) — runway-only crown + crossing blend
USER DIRECTIVE (in-sim, testing crowns): crown RUNWAYS ONLY this
iteration; blend crowns at every runway intersection so centerlines
cross at the same elevation and edges meet smoothly; emit BOTH ridges
continuously through the crossing.  The taxi/service crown code is KEPT
INTACT (evaluation scoping, not removal).

1. **FAMILY SCOPING** (config.py: ``CROWN_RUNWAYS`` / ``CROWN_TAXI`` /
   ``CROWN_SERVICE``, env ``O4_CROWN_{RUNWAYS,TAXI,SERVICE}``; default
   runways-only = 1/0/0; ``ENABLE_SPINE_CROWN`` stays the master gate).
   ``build_crown_drop_field`` gates each family's eligibility on its
   flag; ``runway_crown_drop_m`` returns 0 when runways de-scoped.
   A de-scoped family's nodes carry c = 0.  ``emit_crown_spines`` skips
   a family's ridge when that family is off.  All taxi/service code
   paths remain — re-enable with the env flags.
2. **RUNWAY-CROSSING DRAINAGE DOME** (crown.py ``_crossing_blend_axes``
   + ``_crossing_dome_drop``): inside a crossing influence zone (a
   runway node with ≥2 member axes within ``_XING_INFLUENCE_M`` = 40 m)
   the uniform per-ref drop is replaced by
   ``drop(p) = min_r RUNWAY_CROWN_TRANSVERSE × min(perp_dist_to_axis_r,
   hw_cap_r)`` — 0 on either centerline (both ridges pass through at
   profile level), rising to the min member half-width in the quadrants.
   Outside the zone the node keeps the plain uniform drop (profile
   reconstruction stays simple); the two regimes agree at the boundary
   (an own-edge node ≥ hw_cap from every foreign axis evaluates to its
   own uniform drop → no transition step).  Runway-shadow adoption uses
   the dome at the crossing so shadowed corridor nodes meet the blended
   edge.
3. **CONTINUOUS CROSSING RIDGE** (crown.py runway spine loop): each
   runway ref's axis is now clipped against the UNION of its
   ROLE_RUNWAY pieces + every ROLE_RUNWAY_CROSSING it belongs to, so the
   ridge is ONE continuous breakline THROUGH the crossing (closes the
   v2 gap item).  Both members' ridges meet where the centerlines cross
   (equal altitude per the reconciliation, #4).
4. **CENTERLINE EQUALITY VERIFIED, untouched**: the runway_segments
   centerline-crossing reconciliation already forces both profiles to
   the same ``agreed`` altitude at the crossing.  Probe (CYXY
   02/20×14R/32L): profile[02/20] = profile[14R/32L] = 694.0769,
   DELTA = 0.00 cm.  Not modified.
5. **READERS** (part 30 field/sidecar unchanged): crossing-adjacent
   nodes export their per-node dome value via ``_crown_drop_ll`` →
   sidecar ``crown_drops`` (CYXY runway-only histogram: 0.081/0.113/
   0.114/0.115/0.13/0.147 blended values alongside 0.12/0.15/0.23 per-
   ref uniforms) and the in-memory ``_crown_drop_key`` both readers
   share.  Invariant held: c single-valued per canonical node, 0 at
   seam pins.
6. **RUNWAY WINS over de-scoped-family freeze** (crown.py: new
   ``descoped_frozen`` set): a runway edge vertex SHARED with a
   de-scoped junction was frozen at c = 0 by the junction, leaving the
   runway's own edge stepping at the weld (7.3 % at SPLP).  Now a
   de-scoped crown-family freeze yields to a co-owning runway's drop
   (genuine non-crown owners — apron/terminal/building/boundary/
   groundside — still hard-freeze).  Runway now crowns ALL 22/23 of its
   SPLP ring vertices (was 11).
7. **``extend_field_to_new_ring_nodes`` bug fix**: a post-solve ring
   insert with BOTH flanks uncrowned got a spurious ≤5 cm drop (ring
   non-planarity read as crown; surfaced once taxi de-scoped).  Now
   inserts with ``c_max == 0`` inherit no drop.

## VERIFIED (gates, part 30c)
* Crossing (CYXY, ``tools/full_airport_build.py`` + probes):
  (a) both profiles at the crossing = 694.0769, ≤ 2 cm ✓;
  (b) crown_spine ridge CONTINUOUS through both crossings on both
  centerlines (crossing 1: 02/20 7 on-axis verts @694.06-694.11,
  14R/32L 6 @694.07-694.09; crossing 2: 02/20 6 @693.68-693.73,
  14L/32R 5 @693.72-693.73) ✓;
  (c) quadrant edge nodes carry the min-formula dome (transect
  perpendicular through the crossing: 0.06 cm on the crossed centerline,
  rising smoothly and monotonically to the 11.5 cm cap at the edges) ✓;
  (d) check_grade: within 1 (known apron-#29), cross 0, plane 0,
  steps 0 ✓.
* Runway-only gating: SPLP within 16 (IDENTICAL pairs to gate-off,
  values uniformly lower by the drop), CYXY within 1, HECA within 0;
  plane/cross 0 all three; HECA vertex-to-edge 3 + mid-edge 14 steps
  IDENTICAL to gate-off; HECA break 5888 (gate-off 5824, all-crown
  5895 — quarantined-by-design class).  taxi/service crowned-node count
  drops CYXY 1170 → 112 (0 taxi/junction/service ring keys carry a drop
  beyond runway shadows); only runway ridges emit.
* O4_SPINE_CROWN=0: SPLP + CYXY patches BYTE-IDENTICAL to HEAD gate-off.
* All-families path preserved (O4_CROWN_TAXI=1 O4_CROWN_SERVICE=1):
  CYXY within 1, 1156 crowned nodes (was 1170; the extend-field fix
  removed spurious inserts), taxi ridge emission unchanged (1 at CYXY —
  narrow corridors eroded by the 1 m inner clearance, pre-existing);
  runway ridges now 8 continuous ways (was 31 per-piece fragments).
* fast_suite: EXACTLY the 8 pre-existing failures.  Full suite: the 13
  pre-existing failures exactly.  Zero new.

## OPEN (part 30c follow-ups)
* ``_XING_INFLUENCE_M`` = 40 m is a fixed reach; if a future airport has
  a very oblique or very wide crossing the zone may want to key off the
  member half-widths instead of a constant.
* Taxi/service ridge emission is sparse at narrow airports (the 1 m
  ``_SPINE_EDGE_CLEAR_M`` inner buffer erodes thin corridors) — a
  pre-existing property, only relevant when taxi/service crown is
  re-enabled.

## LANDED (part 30) — crown v2, the agreed architecture
The v1 post-solve edge-drop module is GONE (crown.py rewritten; the
pipeline "SPINE CROWN" block removed).  The crown is now built INSIDE
the construction, one mechanism for runways + taxiways + service roads:

1. **CROWN DROP FIELD** (``crown.build_crown_drop_field``, the single
   source both readers consume): per-CANONICAL-NODE designed drop c ≥ 0.
   * runway / runway_crossing rings: UNIFORM per-ref drop
     ``profiles[ref]['crown_drop_m'] = RUNWAY_CROWN_TRANSVERSE ×
     min(half_width, 30 m)`` (persisted by redistribute; crossings take
     the min over member refs; shared keys min over refs — uniformity
     keeps the reconstructed longitudinal profile untouched), axially
     TAPERED at 1 % toward tile-seam vertices;
   * taxi/service corridor nodes: ``rate × min(lateral-to-nearest-
     same-family-centerline, half_width cap 12 m taxi / 4 m service)``,
     0 on the spine itself (≤ 1 m tol), MIN over owning families;
   * RUNWAY SHADOW: an eligible node ≤ 2.5 m from a crowned runway is
     value-tied to its edge (vertex-push standoff, edge-plane stamps,
     join anchors) → carries the RUNWAY's drop;
   * frozen at c = 0: any non-crown owner (apron/terminal/building/
     boundary/groundside/adopts_apron_grade), tile-seam buckets, seam
     pins, building seats, groundside mouth welds, seam spine anchors;
     4-corner rect rings equalize (min) so planes stay planes.
2. **SOLVER**: the whole route-profile solve runs in UNCROWNED space
   z' = z + c — byte-identical to the pre-crown solve — and the
   WRITEBACK emits z = z' − c (solve.py; same transform wrapped around
   ``final_grade_projection``: add c after seeding, subtract before its
   writeback).  c is single-valued per canonical node ⇒ welds can never
   tear; no freeze sets, no vetoes, no revoke valve.  Post-solve ring
   inserts (planarize / T-welds) join the field VALUE-DERIVED
   (``crown.extend_field_to_new_ring_nodes``: z'-lerp of solve-time
   flanks minus the insert's value; a geometric nearest-node adoption
   read a phantom 4.2 % pair at CYXY).
3. **LAW** (``grade_law.crown_pair_offset`` + the field): every
   within-shape pair re-centres its budget on the crown target —
   ``|Δz − (c_b − c_a)| ≤ Allowance.at(...)`` — evaluated by
   check_grade (sidecar ``crown_drops`` → per-nid map, offset on
   ShapePairConstraint) and grade_graph_validate.within_violations /
   route_band_violations (de-crowned band compare).  Since every crown
   rate ≤ every transverse cap, the re-centred band still contains the
   FLAT surface — the offset can only restore budget, never flag an
   uncrowned patch.  The solver realises the same offsets via the z'
   transform, so the two readers share ONE field and cannot drift.
4. **RUNWAYS HAVE A SPINE**: ``crown.emit_crown_spines`` (called at the
   end of the solve) repopulates ``layout.crown_spines`` from the
   SOLVED route profiles (on-line graph-node elevations interpolated by
   arc, every ~12 m, ≥1 m inside the crowned pavement, ≥0.9 m off any
   ring) and from the persisted (post-flex) runway profiles clipped per
   piece.  to_osm's OPEN-way ``o4_feature=crown_spine`` emission and
   the check_grade skip are unchanged (KEEP list).
5. Fixed in passing: check_grade's sidecar point→nid grid matching used
   a per-point cos(lat) cell size — at lon −135 the integer cell index
   shifted by whole cells and silently missed matches (seam-pin class
   was too sparse to notice; the crown field exposed it).

## VERIFIED (gates)
* O4_SPINE_CROWN=0: SPLP and CYXY patches BYTE-IDENTICAL to HEAD
  gate-off builds.
* Crown ON (tools/full_airport_build.py → check_grade, law-true):
  - SPLP: within 16 == baseline 16 (identical pairs, values uniformly
    lower by the drop); cross/steps/plane 0; runway 02/20 crowned
    ~0.23 m (seam pieces taper to the pins).
  - CYXY: within 1 == baseline 1 (the known apron-#29 1.25 %); v1
    shipped 2.  cross/steps/plane 0.  Probe: ridge-above-edge
    14R/32L ≈ 0.21–0.24 m (1 % × 23 m), 14L/32R ≈ 0.14, 02/20 ≈ 0.09;
    32 crown_spine ways.
  - HECA: within 0 == baseline 0 (v1 shipped 2 marginal); plane/cross
    0; vertex-to-edge 3 + mid-edge 14 steps IDENTICAL to gate-off
    baseline (pre-existing service-road pair -10611/-10065); break
    pairs 5895 vs 5824 baseline (quarantined-by-design class).
  - fast_suite: EXACTLY the 8 pre-existing failures, zero new;
    test_cyxy_spine_zero + test_cyxy_spine_zero_no_bowl PASS.
  - full suite: the 13 pre-existing failures exactly (see commit).

## LANDED (part 30b) — clearance coverage: Pass A3 airside ring-edge
## sweep (fixes USER RULING part-29 #5, HECA terrain spikes)
ROOT CAUSE (recorded part 29): ``clearance.emit_surface_clearance_cuts``
built cuts only off 4-corner rects carrying ``altitude``/hi-lo (Pass B)
and off the taxi CENTERLINE trace (Pass A) — since part 25 every sloped
shape (and, since the unified runway representation, 51 of HECA's 56
runway pieces) emits per-node ``node_altitudes`` polygons, so junction /
apron / service-road edges away from a centerline were INVISIBLE to the
clearance builder.  FIX (clearance.py):
1. **Pass A3 ring-edge sweep**: walk every TERRAIN-FACING exterior-ring
   edge of airside pavement + service roads (station step, flat-shadow
   ceiling, cut-only, daylight, merge/emit — all REUSED from the
   existing strip builder).  Terrain-facing = the point 2 m outward
   (``_RING_PROBE_M``) is not covered by ANY already-emitted shape (the
   unbuffered static union — adjacent pavement / ribbon / building /
   groundside own their band).  Outward normal from RING ORIENTATION
   (the centroid flip is wrong on concave rings).  Per-role bands:
   taxi-family/junction/apron = full wingtip half-width of the nearest
   aircraft-taxi centerline's letter (Pass A2 pocket rule); runway
   family (non-rect pieces only; Pass B/RESA/skirts untouched) =
   Annex-14 strip reach from the row-100 centreline minus the station's
   centreline distance, END edges skipped (``_RING_END_NORMAL_DOT`` —
   RESA/skirt territory); service roads = NEW 15 m roadside band
   (``CLEARANCE_MAX_REACH_M["service"]`` +
   ``CLEARANCE_OBSTRUCTION_THRESHOLD_M["service"]``, STANDARDS.md row).
2. **Shared-mechanics fixes found by the audit**: ``_collect`` keeps
   the polygon PARTS when a self-intersecting strip ring buffers to a
   MultiPolygon (concave rings — a junction-notch spike survived the
   whole-run drop); ``_build_graded_strips`` run-taper borrows the
   run-end altitude so a SINGLE obstructed station between skipped ones
   still emits (apron/service corridors); ``_finalize``'s inner-edge
   snap list now includes service roads.
3. **Perf**: ``_make_strip_alt_resampler`` builds the strip edge/vertex
   STRtree ONCE per finalize (was: rebuilt per emitted piece, twice —
   finalize 38.9 s → 6.4 s once A3 multiplied the strip count).  HECA
   build 143.2 s (HEAD, warm) → 139.5–146.0 s with the fix (±2%);
   Pass A3 itself costs 0.6 s.
VERIFIED (gates):
* ``tools/clearance_spike_audit.py`` HECA: 1,372 samples / 461 clusters
  worst +15.70 m (HEAD baseline this session; the 1,306/443 in the
  part-29 note predates crown v2) → **203 / 119**.  Of the remaining
  203 samples, 179 sit ≤ 1.6 m from an emitted grading surface (the
  deliberate ``_PAVEMENT_GAP_M`` crack between pavement and cut inner
  edge — mesh-constrained on both sides); only 2 exceed +2 m beyond
  that: +7.97 m in ONE apron/service_junction corridor at
  30.116375,31.410066 (strip lost somewhere in finalize — open below)
  and +2.01 m at 1.6 m distance.  Worst baseline cluster
  (30.126175,31.418247, +15.7): now INSIDE cut way -10759, surface
  83.9 m ≤ apron edge 84.0 + 1.0 threshold.
* check_grade: HECA IDENTICAL to baseline (within 0, plane 0, cross 0,
  steps 3+14, break 5895); CYXY within 1 == baseline, rest 0; SPLP
  within 16 == baseline, rest 0.  Clearance roles carry no grade law
  (ROLE_GRADE_LIMITS → None) — unchanged.
* fast suite: EXACTLY the 8 pre-existing failures.  Full suite: the 13
  pre-existing exactly.  NOTE: the new cuts initially flipped
  ``test_cyxy_taxi_e_south_apron_follows_terrain`` to a FALSE pass (its
  bbox scan took a terrain-hugging clearance cut as "pavement
  climbing") — the test now excludes clearance roles from candidates
  and fails honestly again (the solver over-flattening it guards is
  untouched).
* Cut counts: HECA 105 → 114, CYXY 40, SPLP 10.

## OPEN (part 30 follow-ups)
* ONE residual audit spot at HECA (+7.97 m, 30.116375,31.410066): the
  apron/service_junction corridor stations are valid and obstructed
  (verified by replay) but the strip vanishes in the finalize
  union/clip chain — trace region → components → piece processing for
  that corridor if it matters in-sim.
* The 1.5 m pavement-gap crack band (179 audit samples) is bounded by
  constrained edges on both sides; if in-sim needles ever show there,
  the standoff convention (``_PAVEMENT_GAP_M`` + conformance) is the
  thing to revisit, not the sweep.
* Crossings emit no ridge (runway breaklines gap across the resolved
  crossing junction; its surface carries the min member drop) — a
  crossing-aware ridge weave is cosmetic follow-up.
* grade_feasibility_audit.py consumes ShapePairConstraint but ignores
  the new ``offset`` field (reads conservative-strict on crowned
  pavement); teach it |Δz − offset| if its counts start mattering.
* The in-sim eval should look at: corridor crown visibility, the
  runway ridge at thresholds, seam-taper creases at SPLP.

# STATUS — SESSION 20260707 (part 29): KCLT terminal-ramp groundside
# root cause — the airside/groundside EDGE CLASSIFIER was blind (pick up
# here)

## USER RULINGS + REPORTS (2026-07-07, in-sim eval — QUEUED part 30)
1. **SPJC perfect; CYXY great; KCLT classification fix verified by the
   part-29 rebuild** (see below).
2. **SPLP BROKEN → FIXED (part 29)**: "something broke the anchor at
   the seam where it crosses taxiways" — runway and apron OK.
   ROOT CAUSE (verified with per-tile probe builds): the THRESHOLD
   uniform-lift reconciliation (runway_segments) samples each CIFP
   threshold's surrounding DEM — for a cross-tile runway the far
   threshold is OUTSIDE the current tile's raster and ``dem.alt``
   silently CLAMPS to the edge column, so each tile build computed a
   different mean lift (−77 build: +1.9 m, using seam-column terrain
   for the west threshold 882 m into tile −78).  The divergent
   profiles (5.05 m worst) fed ``runway_clamp_floor`` → taxiway/
   junction seam pins 1.45 m apart across the 10 m gap = the in-sim
   scarp.  The pokes-above (+0.05 m) seam-anchor filter amplified it
   (one build kept the runway seam anchor, the other dropped it, so
   thresholds shifted on one side only).
   FIX: covering-raster rule in ``_sample_dem_ll`` — out-of-tile
   points sample the raster that covers them (``_load_airport_dem``,
   cached, graceful None fallback).  Profiles now bit-identical
   across tile builds; worst cross-tile seam delta 1.48 m → 0.09 m.
   Fast suite: same 8 pre-existing failures, zero new.
   ``O4_SEAM_DEBUG=1`` dumps per-build seam-anchor decisions in
   runway_redistribute (kept, env-gated).
   NOTE: the 2 ``test_compare_target_splp`` failures are a DIFFERENT
   (structural apron-matching, pre-existing) issue — not this.
3. **Lateral crown law (NEW, durable)**: everything with a SPINE —
   runways, taxiways, service roads — must crown for drainage: spine
   slightly higher than the edges, with PER-ROLE transverse-grade
   values researched from FAA AC 150/5300-13 / EASA CS-ADR-DSN and
   cited in docs/STANDARDS.md (constants in config.py).  Symptom being
   fixed: ridges/valleys along service-road spines at several
   airports (lateral grading currently unconstrained there).
   USER RULING (2026-07-07, after 29b review): the crown should NOT be
   a special post-solve module — it belongs in the GRADING itself:
   spine-vs-edge allowances.  AGREED DIRECTION for part 30 (the 29b
   module is a working v1; its freeze/veto/valve machinery exists only
   because it fights the solve after the fact):
   1. Level-coupling: the solver already couples lateral corridor
      nodes to their spine (the "8,548 lateral corridor node(s)" pass
      + solver_primitives' level-coupling graph) at OFFSET 0 — couple
      at −rate·lateral instead.  Welds then solve consistently by
      construction (no freeze sets, no consensus tears).
   2. Law: add an OFFSET field to grade_law.Allowance so spine↔edge
      pairs budget |Δz − crown_offset| ≤ cap·d; solver and validator
      read the same object → the validator CHECKS the crown;
      infeasible pockets go through break-region, replacing the
      revoke valve.
   3. Emission: spine breaklines from the SOLVED route profiles
      (routes already carry elevations — the axes sidecar) instead of
      ring interpolation; crown.py shrinks to that emission step.
   4. Runways: profile stays spine authority, edges derive inside the
      solve → the runway_join/flex/skirt readers see law-consistent
      values (the 29b runway exclusion should lift naturally).
   IMPLEMENTED (part 29b, superseded-in-place by the above plan):
   - ``crown.py`` ``apply_spine_crown`` (pipeline, after the final
     projection, before skirts; gate ``O4_SPINE_CROWN`` default ON):
     taxi-family corridors/rects (1 %) + service roads (1.5 %) drop
     their edges below the spine; spine breaklines emit as OPEN ways
     with per-node alt_abs (+ ``o4_feature=crown_spine``; check_grade
     skips them).  Axis fallback = longest apt.dat centerline through
     the shape (clip passes strip source_axis).  SAFETY MODEL: freeze
     everything not crowned (other roles, axis-less family shapes,
     MultiPolygon/holed shapes, seam vertices), register ZERO drops so
     near-axis owners veto neighbours' drops at shared vertices,
     budget-aware all-pairs Lipschitz smoothing at min(cL,cT)·d (a
     provable lower bound on any law allowance), and a revoke-valve
     that un-crowns any shape whose final values would still violate.
   - Law: ``_bake_edge`` cT — service-road-rate pairs now cap at
     SERVICE_ROAD_MAX_TRANSVERSE (2 %) instead of tilting at their 5 %
     longitudinal cap.
   - VERIFIED: SPLP within 16 == baseline; CYXY 2 (known apron-#29 +
     one at +0.16 %); HECA 2 (both ≤ +0.08 % over 60-95 m chords),
     break 5822→5811; fast suite = the 8 pre-existing failures
     exactly.  RUNWAYS EXCLUDED this slice: crowned runway corners
     broke the runway_join spine check (23 % step at CYXY) — the
     profile readers (join anchors, flex audit, skirts, seam pins)
     must learn the crown offset first; profile-axis wiring in
     crown.py is ready.  Also queued: SPLP corridors stage 0
     breaklines (narrow shapes + the 0.9 m ring-clearance filter).
   DESIGN GROUNDWORK (part 29, retained for the runway leg):
   - **Mesh unlock**: ``include_patches`` (O4_Vector_Map ~line 1115)
     inserts OPEN (non-closed) patch ways as constrained DUMMY
     breakline edges honouring per-node ``alt_abs`` — so a TRUE crown
     ridge needs NO polygon splitting: emit each spine as an open way
     at ``surface_at(station) + crown_rate × local_half_width``,
     tapered to 0 over the last ~half-width before shape ends (mouth
     continuity).  Runway sub-rects are planes → spine alt = plane at
     centerline + crown.
   - **Law side**: the anisotropic machinery already exists —
     ``grade_law.Allowance(cL, cT)`` + ``grade_graph._bake_edge``,
     which today sets cT = TAXI_MAX_TRANSVERSE_NARROW (2 %) only for
     A/B narrow taxi pairs and leaves everything else isotropic
     (service roads laterally capped at their 5 % LONGITUDINAL cap =
     25 cm across a 5 m road — the visible ridge/valley budget).
     Extend cT per role;  OPEN QUESTION to trace first: how
     service-road pairs actually flow through ``classify_pair`` /
     ``_edge_route`` (the ``both_road`` relax path vs spine_caps) so
     the transverse cap binds the RIGHT pairs on both readers.
   - Constants to add (pending the research table below):
     RUNWAY_{MIN,MAX}_TRANSVERSE, TAXI_{MIN,MAX}_TRANSVERSE (per
     letter; NARROW A/B 2 % exists), SERVICE_ROAD_{MIN,MAX}_TRANSVERSE
     + per-role CROWN rate; docs/STANDARDS.md rows with citations.
4. **Service-road adoption extension (durable)**: like the apron-edge
   rule, the PORTION of a service road inside or sharing a LONG edge
   with a taxiway follows the more limiting (taxiway) grade law; only
   isolated narrow-road stretches (nothing along the long edge) get
   the full 4 % road cap.
5. **Clearance coverage** — LANDED part 30b (see above): several spots
   at HECA show small terrain
   spikes right next to pavement — the clearance cuts miss them.
   ROOT CAUSE FOUND (part 29, fix queued): ``clearance.
   emit_surface_clearance_cuts`` builds cuts only off ``_usable``
   shapes = 4-CORNER rects carrying ``altitude`` or hi/lo — but since
   part 25 (hi/lo emission retired) every sloped shape emits per-node
   polygons, so junctions, aprons, and service roads are INVISIBLE to
   the clearance builder.  That matches the audit exactly (spikes
   cluster beside apron/junction/service edges).  FIX SHAPE: extend
   the pass to walk every airside ring edge with per-node altitudes
   (generalising the two-long-edge rect walk), cut-only as today.
   TOOLING: ``tools/clearance_spike_audit.py`` (committed) turns the
   report into worst-first coordinates — HECA baseline 1,306 samples /
   443 clusters, worst +15.7 m at 30.126175,31.418247; use it as the
   before/after gate for the fix.

## USER REPORT (part 29)
KCLT (in-sim/JOSM after part 28): complex mess of jagged shapes around
the central terminals + spurious groundside that should not exist.
HECA fine.  Root-caused to ``_terminal_groundside_zone`` (terminals.py)
misclassifying concourse-facing RAMP edges as groundside → 482,579 m²
subtracted (incl. the whole Concourse E ramp), which also SEVERED the
pavement graph → 96 shapes demoted by the runway-disconnected pass.
Three stacked blindnesses, each verified at KCLT:
1. **Nimbus KCLT apt.dat has ZERO row-110 pavement** (all pavement is
   DSF-draped) → ``apt_only_pav_polys`` empty → the airside-reachability
   BFS degenerated to "within 100 m of a runway" — never true at a
   terminal.  FIX (pipeline.py): fall back to the FULL pavement list
   (incl. DSF polys) when the apt-only snapshot is empty; airports with
   real row-110 keep the apt-only list bit-for-bit.
2. **OSM aprons at KCLT are multipolygon RELATIONS** (member ways carry
   no tags) → invisible to the ways-only aeroway catalog.  FIX
   (terminals.py): reconstruct matching relations' rings (closed
   members direct, open members polygonized — same pattern as
   ``_extract_osm_terminals``) into the airside catalog; new
   ``relations=`` param, single call site.
3. **Tag-set gap**: the real OSM tag is ``taxilane`` (the set only had
   ``taxi_lane``, which does not occur in OSM) and ``jet_bridge`` was
   missing — 159 + 124 such ways at KCLT concourses alone.  With all
   ramp edges UNKNOWN, the any-airside promotion turned them ALL
   groundside (100 m rectangles = the jagged sawtooth).

## VERIFIED (KCLT rebuild, gz-probe + coverage-probe instrumented)
- ground-zone subtraction 482,579 → 110,312 m² (genuine curbside only);
  all 7 probe points now airside end-to-end (1 stays groundside — a
  REAL pavement island 7 m off the apron, correct per the 2026-06-09
  island ruling).
- runway-disconnected demotions 96 → 9 (rest are road-served pockets).
- terminal zone (700 m): groundside 14 shapes/72.6 k m² → 1/2.6 k m²;
  aprons 48 fragments (median 2.2 k m²) → 13 shapes (median 36.6 k m²).
- check_grade: within 5 → 6 (all on one apron, worst +1.57 % excess,
  apron -10074 — new terrain the restored ramp must now grade over),
  break-region pairs 614 → 365, steps 4 → 3, plane/cross still 0.
- fast_suite: identical 8 pre-existing failures on stashed HEAD and on
  the fix — zero new failures.  HECA rebuilt: see scoreboard below.

# STATUS — SESSION 20260706 (part 28): KCLT keep-all-pieces + apron-edge
# service adoption + FLEX minimum-displacement

## USER RULINGS (part 28)
1. **Apron-edge service grading (PORTION-based, clarified)**: the portion
   of a service road/junction inside or alongside an apron follows APRON
   grading; the portion beyond the mouth grades at service rules.
   Implemented: apron-edge adoption pass (pipeline; split at the apron
   band = SERVICE_ROAD_WIDTH_M + 2 m), ``BuiltShape.adopts_apron_grade``
   → solver caps (_shape_grade / _body_cap / rect plane path) →
   ``o4_grade_law='apron'`` tag → check_grade override + OSM GradeShape.
2. **Flex = minimum displacement, taxi at max cap first** (user
   directive after in-sim: runways bending/dipping too much).

## FLEX FIX (root-caused end to end; tools/flex_audit.py verifies)
Chain of defects fixed in ``_apply_runway_flex_hook``:
- Sequential rounds let the FIRST runway absorb the whole inter-runway
  deficit (HECA 05C measured **17.8 m** one-sided) → rounds are now
  SNAPSHOT-SIMULTANEOUS.
- Demands now carry envelope ORIGIN (which runway pulls); a demand whose
  binding seed is another flexible runway moves **deficit/2** so the
  profiles meet in the middle (user's deficit÷runways formula);
  immovable origins (seam/CIFP) keep the full move.
- **RUNWAY_FLEX_MAX_DISPLACEMENT_M = 4.0** (config): cumulative budget
  per profile vs the pre-flex original.
- Shared-vertex propagation: flexed runway values re-stamp coincident
  vertices on neighbouring shapes + the solver seed (stale junction
  values were re-imposed through the shared bucket at writeback).
- The runway-join anchor loop no longer overrides flexed runway hard
  nodes (comment said "never override", code did).
- ``_sample_runway_segment_elev``: least-squares PLANE FIT replaced by
  axis-projected interpolation (diameter axis, NOT the bbox diagonal —
  that broke SE-heading runways, SPJC 16L 0.4 m anchor errors); the
  plane fit extrapolated ~3 m wrong on flexed (curved) pieces.
- GOTCHA that cost two diagnosis rounds: the pipeline wraps the hook in
  a blanket ``except`` → an IndexError (interpolating ORIGINAL elevs
  against the sample-mutated fractions) left builds HALF-FLEXED with
  only a one-line WARN.  Grep ``flex pass failed`` before trusting any
  flex measurement.
- VERIFIED (HECA flex-on): 0 within/0 plane/0 cross; ±4.00 m max
  displacement, bidirectional; audit shows zero taxi-not-at-cap
  clusters; 110 of 441 m demand drained, rest quarantined on the taxi
  side per FLEX-LAST.  SPJC 0 within (1 break), CYXY 1 within (the
  known #29 open).

## KCLT (user in-sim report) — fixed
- ``_drop_overlap_against_fixed_shapes`` kept only the LARGEST clip
  piece → the far side of every runway crossing was deleted (one-sided
  spines, ~6.6 k m² true loss).  All pieces ≥5 m² now survive as their
  own shapes and re-enter the fixed-point clip loop.
- The 50 m cut's near-zone counted GATE LEAD-IN taxilanes → 63 stand
  aprons re-roled whole → apron-island merges lost their hosts → the
  terminal ramp demoted to DEM groundside.  Zone now uses THROUGH
  routes only (each end joins another centerline or the runway).
- Cut pieces exempt from ``_drop_off_source_residue``
  (``from_route_proximity_cut`` flag).
- All 7 user coordinates verified restored (2 were already missing in
  the PRE-part-27 baseline and are now recovered); pt3 disc coverage
  0.27 → 0.73 (small residual notch remains).

## TOOLING RULING: persistent tools live in tools/ (NOT /tmp — it purged
## twice mid-session).  tools/full_airport_build.py, tools/flex_audit.py.

# STATUS — SESSION 20260706 (part 27): classification rulings landed;
# weld-authority machinery built; 3 open residuals

## USER RULINGS THIS SESSION (durable law)
1. **No apron may ever touch a runway** — memory
   apron_never_touches_runway_ruling.md.
2. **50 m route-proximity law**: pavement within 50 m of a taxi
   centerline or runway is NOT apron; beyond 50 m may be.  Enforced by
   the APRON ROUTE-PROXIMITY CUT (pipeline.py, config
   APRON_ROUTE_PROXIMITY_M): every ROLE_APRON shape is SPLIT at the
   50 m contour (taxi non-service centerlines ∪ runway union, mitre
   buffers) — near band → ROLE_JUNCTION, far part stays apron.  The
   original report (30.1142593,31.4157106): slice-corridor cells were
   flipping to apron via _reclassify_apron_junctions' whole-shape 55 m
   rule + neck-split pieces inheriting apron unconditionally.

## LANDED (all default-on; measure before trusting numbers elsewhere)
- **reclassified_from_junction flag** (layout.py/junction_repair) +
  neck-split piece re-eval (pipeline) — corridor cells return to
  junction.  HECA break-region 11,243 → ~1,334 (gate-off, −88 %).
- **Apron route-proximity cut** (pipeline, after neck-split).
- **Lot↔lot weld reconciliation** at reach time (anchors.py,
  O4_GS_MOUTH_RECONCILE): smaller lot adopts larger's ±cap·d band,
  ABSOLUTE Lipschitz cone (relative cones under-raise at-cap rings —
  measured 4.00 %→4.64 %).
- **Mouth VERIFY-AND-RELAX** post-yield (solve.py,
  O4_MOUTH_VERIFY_RELAX): pad/apron-conflicted mouth welds join the
  joint solve (pads move AFTER the reach-time welds — reconciling
  earlier chases stale values, measured +0.8 m WORSE); lots adopt the
  solved profile (adopt_projected_mouths, NO chord-limit — the
  downward limiter dragged an adopted mouth 2.1 m).  Freed mouths +
  still-contradictory weld↔weld edges → break export.  HECA #541/#546
  FIXED, #522 quarantined.
- **Service-road proximity coupling** (anchors dem_follow,
  O4_SVC_PROXIMITY_COUPLE, 2 m) + **parallel-edge conformance**
  (groundside.conform_parallel_service_edges, O4_SVC_PARALLEL_CONFORM)
  + DEM-follow break-blend export (layout._service_break_idx).
- **Triangle-plane law** (solve._project_triangle_planes,
  O4_TRIANGLE_PLANE_LAW): 3-vertex shapes' plane gradient clamped via
  the freest vertex within its law-edge interval; unfixable → break
  export; validator plane check + STEP checks now consume the break
  quarantine (check_grade).  HECA plane 2 → 0.
- **Terrain-pinned pair export** (final projection): violated edges
  touching seam/feature-weld pins (incl. 0.5 m-tolerant weld-key grid +
  torn-weld set) → quarantine.  Post-projection groundside re-limit +
  ribbon/bridge re-adoption + moved-weld quarantine (pipeline).

## SCOREBOARD at session end (gate-off, per-airport lab builds)
- SPJC 0 within + 0 break + 0 steps ✓
- HECA 0 within + 0 plane + 0 cross; ~1,334 break; **3+14 steps OPEN**
  (#578↔#64: two parallel service roads 1 m apart, 0.9 m wall — the
  DEM-follow blend did NOT fire there; O4_SVC_DEBUG_LL=30.102180,31.395020
  instrumentation is in anchors.py, /tmp/heca_final1.log has the dump).
- CYXY **1 within OPEN** (apron #29 pair 1.25 %, 0.25 % excess,
  693.56↔692.85 over 57 m at 60.714896,-135.064193 / 60.715385,-135.064502).
  DIAGNOSED DEEP: the 692.85 vertex is a boundary-bridge contact
  inserted POST-SOLVE (absent from the solve node list — nearest solve
  node 30 m away); at final-projection END the pair was LAWFUL
  (693.56/693.00 = 0.98 %) — something between writeback and emit
  restores 692.85 (bridge value).  The node has only 6 joint edges (a
  41-vert apron ring should give ~40) — the projection's lazy tier
  under-covers the shape in BOTH scoped and full paths
  (O4_SCOPED_FINAL_PROJECTION=0 A/B identical).  Feature-weld agreement
  gate never sees the bridge vertex (absent from feat_alt_by_key even
  with the 0.5 m grid).  NEXT: find who writes 692.85 after writeback
  (suspect emit consensus merging with the bridge ring vertex that the
  post-projection cascade also cannot see), and why the apron's
  constraint entry is ring-adjacent-only.
- **SUITE 12F/409P** (base10 was 10F/407P) — composition:
  * FIXED vs base10: test_cyxy_spine_zero, test_cyxy_route_reach_zero,
    test_cyxy_spine_zero_no_bowl (all three CYXY spine gates GREEN).
  * NEW: compare_target ×3 (SPJC + SPLP both tiles — the cut/
    classification changed geometry; recut fixtures with
    tools/build_target_osm.py ONCE the opens settle),
    test_pavement_grade[CYXY] (open residual above),
    test_pavement_grade[SPJC] (open 0.61 m pad step above).
  * Still failing from base10: HECA/SPLP grade + longitudinal (SPLP =
    flex Stage C), SPJC route-band/self-overlap, CYXY terrain-follow,
    solver_validator_same_edge_budgets.
  base10.txt kept for reference; do NOT recut until the 3 opens close.

## TOOLING (user 2026-07-06: persistent tools live in tools/, NOT /tmp)
- tools/full_airport_build.py — the lab build runner (replaces
  /tmp/spjc_lab/full_build.py; regenerate-in-/tmp notes are obsolete).
- tools/flex_audit.py — flex-on vs flex-off displacement map + binding
  taxi-axis slack per flexed cluster (FLEX-LAST verification).

## GOTCHAS ADDED
- Debug envs: O4_SLIVER_DEBUG (cut), O4_SVC_DEBUG_LL=lat,lon
  (dem_follow reach state), O4_PROJ_DEBUG_LL=lat,lon;lat,lon (final
  projection node state), O4_STEP_DEBUG prints [mouth-relax] /
  [terrain-scan] / [triangle-plane].
- _aeroway_centerlines_union carries runway axes ONLY for 4-corner
  runway rects — HECA's multi-segment runways contribute none (the
  cut uses runway_union directly for this reason).
- check_grade: plane + STEP sections now quarantine via break_nodes
  (same _touches_break_node as pairs; steps use vert_pt/proj_pt at
  2 m tolerance).

# STATUS — SESSION 20260706 (part 26): HANDOVER — HECA-to-zero plan
# (superseded by part 27 above)

## State at HEAD (all gates green, suite 10F == /tmp/base10.txt)

Actionable scoreboard (gate-off defaults): **CYXY 0 · SPJC 0 · SPLP 0
per-tile · HECA 2 within + 2 plane**.  With `O4_RUNWAY_FLEX=1` at
HECA: 11 actionable + 2,762 break-region (was 11,265 frozen).  KDFW 41
(not re-measured since the runway rework — remeasure before trusting).
ONE altitude representation end to end: per-vertex from creation,
per-node in the OSM (hi/lo + cell_size retired, `3383040`); base
Ortho4XP src untouched.

## AWAITING USER: in-sim visual of flexed HECA
FLEX IS NOW DEFAULT ON (part 27) — just restart Ortho4XP + rebuild
the tile.  Spots:
05L↔05C corridor aprons (30.131691,31.410624; 30.126324,31.413003 —
former 2 % quarantine blends), 05C midfield dip (~30.1073,31.4077).
Verdict gates Stage C + default-on.

## HECA-TO-ZERO PLAN (test_pavement_grade[HECA] measures GATE-OFF)

The 4 gate-off pairs, fully diagnosed this session:
1. **service_road #541 weld-authority conflict ×2** (18 %/2.78 m,
   98.87↔99.38): building22 pad weld vs groundside mouth weld, both
   values-AGREED (hard by the weld gate) but mutually conflicting — a
   0.51 m ramp needs 10 m at 5 %, the sliver has 2.78.  FIRST PROBE:
   why didn't `apply_service_road_dem_follow`'s break blend fire (both
   ends are its anchors; floor>ceil should blend + export)?  Fix
   ranked: (a) mouth reconciliation — ONE authority per mouth (the P4
   flush-weld machinery should make the lot adopt the pad-adjacent
   level); (b) road-graph break blend + export (honest quarantine);
   (c) re-role/merge the sliver.
2. **2 plane-gradient pairs** (apron 2.4 % @78.55 by building7;
   junction 6.8 % @115.81 at runway 05C corners): triangle-surface
   check — read `_check_plane_gradient` semantics first; likely shared-
   corner consensus/co-location at the same weld neighborhoods.
Then: green test → recut /tmp/base10.txt → base9.

## FLEX ARC (after in-sim sign-off)
- **Stage C**: intermediate anchors JOIN the solve — the fold-in
  currently freezes crossing/shape-vert anchors at old values;
  release them iteratively within the runway law.  Covers the SPLP
  displaced-02-threshold (fixes test_runway_longitudinal[SPLP]) and
  should eat into HECA's 2,762.
- Provenance tool for remaining pockets (which anchors bind).
- Default-on gates: KDFW/CYUL/SPLP counts + suite; flip
  O4_RUNWAY_FLEX default; then reconcile the flex-on HECA 11.
- Machinery map: docs/runway_flex_plan.md; hook =
  solve.py::_apply_runway_flex_hook; profile ops =
  runway_redistribute.apply_runway_flex / flex_slack_at (greedy-keep +
  verify-and-relax are load-bearing — see commit cddd950/558e000).

## TEST-ZERO CAMPAIGN REMAINDER (tasks #9-13, exact assertions in
## the 2026-07-06 inventory)
- CYXY spine-47 (5.9 % vs 5 % junction spines) — clears 2 tests.
- CYXY budget lockstep (6/6919 edges, all at one vertex, ~2 % drift).
- CYXY route-reach 4 + terrain-following (SW region 708.4 vs 714 —
  the OPEN spine-rise-to-region item).
- SPJC self-overlap (2 pairs, 9.6 m²) + route-band 3.
- SPLP seam-cut conformance hairline (parallel chains 0.8 m apart,
  9 cm) + the longitudinal red (→ Stage C).

## GOTCHAS FOR THE NEW SESSION
- /tmp gets purged: recreate /tmp/spjc_lab/full_build.py (template in
  this session's transcript) + /tmp/base10.txt (regenerate: full suite
  → FAILED lines sorted).  SPLP is measured PER-TILE.
- Probe frames: check_grade._ll_to_m_factory without anchor= is the
  MEAN frame; layout probes convert lat/lon via layout.ll_to_m.
- solver_primitives.SLOPING_RECT_ROLES ≠ junction_rules' same-named
  tuple (solver's includes service_road).
- ORDERING LAW (×3 now): every value-moving or geometry pass runs
  BEFORE final_grade_projection; anything after must be law-guarded.

---

# STATUS — SESSION 20260706 (part 25): hi/lo + cell_size emission
# RETIRED entirely (`3383040`) — one altitude representation everywhere

> to_osm's last hi/lo emitters (boundary ribbons, tunnel ramps,
> taxiway_clearance, stray 4-corner aprons) now ship per-node alt_abs;
> no-consensus fallback = way-level node_altitudes tag; near-planar
> VALUE collapse kept as smoothing (emitted per-node);
> canonicalize_high_low_ring / _slope_profile_for / cell_size+profile
> imports deleted; test_layout invariants flipped (no legacy slope
> way-tags, per-node values preserved).  Zero legacy tags in patches.
> GATES: CYXY 0 / SPJC 0 / HECA flex 11+2,762 hold; suite 10F==base10.

---

# STATUS — SESSION 20260706 (part 24): runways per-vertex from BIRTH +
# per-node OSM emission (`a7de0f6`, user rulings)

> (1) Creators fixed (elevation.py segment emit — also legacy 0.1 m
> rounding retired — + tile_cut clean-rect): node_altitudes from
> construction; normalize sweep = INVARIANT ALARM only.  (2) to_osm's
> hi/lo compaction skips the runway family — all sloped runway pieces
> emit per-node alt_abs (exact + human-editable; parser renders planar
> quads identically).  Supersedes 2026-05-23 keep-rects for runways.
> GATES: HECA flex 11/2,762 holds; CYXY 0, SPJC 0; suite 10F==base10.
> HECA honest state with flex ON: 11 actionable (= pre-existing
> service-road weld cluster + small aprons, task #14 — NOT flex
> fallout) + 2,762 quarantined (genuine residual terrain demand;
> Stage C + provenance next).

---

# STATUS — SESSION 20260706 (part 23): runways UNIFIED to per-vertex
# node_altitudes mid-pipeline (`acd254a`)

> **USER QUESTION ('still rects on runways?')**: taxi network was
> already unified; runways were the holdout.  Now per-vertex
> EVERYWHERE mid-pipeline: _apply_profile_to_shapes always per-vertex;
> normalize_runway_altitudes sweeps stragglers + clears stale dual
> attrs; _sample_runway_segment_elev PLANE-FITS per-vertex pieces (old
> nearest-vertex degraded clearance/anchors).  EMIT unchanged by
> design: to_osm still compacts near-planar quads to hi/lo TAGS (user
> 2026-05-23 'keep rects at emit' ruling; canonicalize_high_low_ring
> rotates inverted rings correctly).  SPJC + SPLP-77 fixtures recut.
> Suite 10F==base10; HECA flex 11/2,759 holds.

---

# STATUS — SESSION 20260706 (part 22): flex tear = [H,L,L,H] slope-
# INVERSION bug (`558e000`) — HECA quarantine −75 %, READY FOR SIM TEST

> **USER CORRECTION**: HECA has no crossings — the 'crossing seam'
> attribution was wrong.  Trace found the real bug: the canonical-rect
> 'ensure hi is higher' swap MIRRORS a piece whose slope the profile
> inverted (flex dips do this routinely; latent since the seam
> redistribute).  Inverted pieces now convert to node_altitudes.
> **FLEX SCOREBOARD (O4_RUNWAY_FLEX=1 at HECA)**: actionable 11 (ZERO
> runway pairs; the 11 = pre-existing service-road weld cluster),
> quarantine 11,265→2,759 (−75 %), 57 demands / 200 m drained.
> Gate-off: CYXY 0, suite 10F==base10.  Patch for the user's X-Plane
> eyeball: /tmp/HECA_flexb2f.osm; production = O4_RUNWAY_FLEX=1 +
> restart Ortho4XP + rebuild HECA tile.  In-sim spots: 05L↔05C corridor
> aprons (30.131691,31.410624; 30.126324,31.413003), the flexed 05C
> midfield (~30.1073,31.4077 — was the worst tear, now clean profile).
> NEXT (Stage C, after visual sign-off): intermediate anchors join the
> solve properly (SPLP displaced-threshold; remaining 2.7 k quarantine),
> then default-on gates at KDFW/CYUL/SPLP.

---

# STATUS — SESSION 20260706 (part 21): FLEX B2 SHIPPED gate-off
# (`cddd950`) — HECA quarantine −60 %, awaiting in-sim visual test

> **B2 (envelope demands over the whole profile)**: 59 demands drain
> 250 of 360 m across all three 05-families; HECA break-region
> 11,265→4,498; projection over-cap 12,086→6,160.  Three hard-won
> mechanisms: GREEDY-KEEP target consistency (forcing dragged small
> flexes past their slack → 2.8 m runway-internal steps), VERIFY-AND-
> RELAX (jointly-infeasible target sets midpoint through
> faa_hard_cap_pass → drop nearest target, re-solve from originals),
> shape-vert fold-in as anchored intermediates.  Actionable 2→32 —
> concentrated at RUNWAY-CROSSING seams: _resolve_runway_crossings
> (elevation.py, pre-solve) interpolates crossing junctions from
> PRE-flex profiles; shared verts re-impose post-flex.  **Stage C =
> crossing values join the solve** (the ruling already covers this).
> Test patch: /tmp/HECA_flexb2e.osm.  Gate O4_RUNWAY_FLEX default OFF;
> enable for the in-sim build (restart Ortho4XP first — module cache).
> In-sim look: the 05L↔05C corridor aprons (previous break spots
> 30.131691,31.410624 / 30.126324,31.413003) and the crossing area
> (~30.1073,31.4077 worst-tear neighborhood).

---

# STATUS — SESSION 20260706 (part 20): RUNWAY FLEX Stage B v1 built +
# measured (`3a3d761`, gate OFF) — Stage B2 = envelope-level demands

> **Stage B v1 (contact-pair flex)**: drains HECA's full 8.50 m contact
> deficit (2 pairs, 4 contacts, 05C/23C+05L/23R) but quarantine only
> 11,265→10,838 and actionable 2→9.  FINDING: pocket contradictions
> press against the WHOLE profile (every runway node is a hard envelope
> anchor), not just taxi-join contacts.  **Stage B2**: per-profile-
> sample [floor,ceil] demands from the max-cap graph (rest-of-field
> certain anchors), profile re-solves against interval targets through
> faa_joint_solve — equivalently runway interiors join the field solve
> interval-constrained with runway-law edges along the axis.
> Machinery in place: apply_runway_flex / flex_slack_at (certain-anchor
> slack) / _apply_runway_flex_hook (budget Dijkstra between contacts).
> Gate O4_RUNWAY_FLEX default OFF until B2.

---

# STATUS — SESSION 20260706 (part 19): seam ruling landed (`95347fb`),
# RUNWAY FLEX plan ratified + Stage A measured (docs/runway_flex_plan.md)

> **USER RULINGS**: (1) seam values sample the SMOOTHED DEM everywhere
> — alt_strict retired from the runway path; memory entry
> seam_values_smoothed_dem_ruling.md.  Honest exposure: SPLP -77
> per-tile 0→16 (the anchor pair was always infeasible; sampler made it
> visible); compare-target fixtures recut.  (2) RUNWAY FLEX approved:
> only CIFP thresholds + seam anchors are CERTAIN; intermediate anchors
> SOLVED; **FLEX-LAST** — the runway moves only when taxiways are at
> max cap, by the minimum (= distance to the max-cap reach interval).
> **STAGE A RESULT (flex-demand map, scratchpad flex_demand_map.py)**:
> HECA has 12 runway-contact anchors and only **2 infeasible contact
> pairs at max-cap budgets — both 05C/23C ↔ 05L/23R, worst deficit
> 7.67 m** (contacts 115.77 vs 60.42) + one 0.20 m.  The entire 11k-pair
> quarantine reduces to ~7.7 m of inter-runway deficit between two
> profiles.  Stage B (two-pass profile flex, O4_RUNWAY_FLEX) targets
> exactly this.  SPLP's displaced-threshold case = Stage C.

---

# STATUS — SESSION 20260706 (part 18): **SPJC = 0** — sliver-needle
# repair moved pre-projection (`8716f88`); SPLP/HECA residuals mapped

> **SPJC LAST PAIR**: the emit-time needle repair ran AFTER the final
> projection — two lawful blend sub-edges merged into one 77 m ring
> edge nobody enforced.  repair_sliver_corners now runs pre-decimation;
> emit scan stays as the quantization-born backstop.  SPJC 1→0,
> test_pavement_grade[SPJC] GREEN — suite 10F/407P (base10 recut).
> **SPLP RESIDUALS (mapped, parked)**: (1) longitudinal 1.61 % = two
> IMMOVABLE anchors (interior anchor k=5@48.82 + seam raw-HGT anchor
> k=8@61.00, 770 m apart = 1.58 % mean; threshold shifting can't touch
> interior anchors; seam value non-negotiable per preserve_boundary) —
> DESIGN DECISION: quarantine runway break segments vs renegotiate the
> interior anchor.  (2) cross=2 hairline: two parallel boundary chains
> 0.8 m apart (the logged SPLP residual T-junction) valued 9 cm apart —
> conformance work at the seam cut.
> **HECA RESIDUALS (mapped, parked)**: 2 within + 2 plane = agreed-weld
> authority conflicts (building pad 99.38 vs groundside mouth 98.87
> across a 2.78 m road sliver = 18 %/0.5 m; two groundside welds at
> 5.3 % marginal) + 2 tiny plane-gradient pairs.  NOT auto-quarantining
> both-hard pairs: the last two both-hard classes (SPLP clamp floor,
> HECA weld gate) were REAL anchor bugs the actionable count exposed.
> **SCOREBOARD at 8716f88**: CYXY **0**+320 · SPJC **0**+0 · SPLP
> **0** per-tile (profile+hairline live in other tests) · HECA
> 2+2plane+11265.  Campaign start: 28/56/15/104.

---

# STATUS — SESSION 20260706 (part 17): **HECA 16→2** — feature-weld
# hardening requires value AGREEMENT (`2c7e561`)

> **HECA CLIFF CHAIN (3 dynamic probes)**: solve-phase envelope lift
> (uphill hard-anchor floor along the service network) left road nodes
> +3.4 m; groundside minted mm-coincident raw-DEM verts; the final
> projection's feature-weld rule FROZE the damaged nodes ("welded to
> emitted features") → 16 both-hard walls reported "genuine".  The
> rule's rationale (feature ADOPTED pavement value) only holds when the
> sides AGREE — hardening now derives the feature vertex altitude and
> requires |Δ| ≤ 0.05 m; torn welds stay FREE.  Unverifiable feature
> altitudes stay conservatively hard.
> **SCOREBOARD at 2c7e561**: CYXY **0**+320 · SPJC **1**+0 · SPLP
> **0** per-tile · HECA **2**+11263 (one 0.5 m step ×2 on road #541 —
> same neighborhood, small residual).  Suite 11F/406P == base11.
> Campaign start (2026-07-05) was 28/56/15/104.
> **PROBE-FRAME GOTCHA (again)**: check_grade._ll_to_m_factory without
> anchor= is the MEAN-of-nodes frame — layout-frame probes must convert
> via layout.ll_to_m from lat/lon.
> **NOTE**: solver_primitives.SLOPING_RECT_ROLES ≠
> junction_rules.SLOPING_RECT_ROLES (the solver's includes
> service_road; the geometry one doesn't) — same name, different
> contents, easy to misread.

---

# STATUS — SESSION 20260706 (part 16): **SPJC 5→1** — validator
# route_zone gap + tunnel-ramp inner-edge lerp (`52fff98`)

> **VALIDATOR ROUTE-CONTACT GAP**: _grade_context_from_osm never built
> route_zone, so the emitted-OSM reader refused APRON_ROUTE_CONTACT
> budgets the solver lawfully granted (SPJC apron #188).  Now mirrored
> from the emitted route-role ways.
> **TUNNEL RAMP INNER EDGE**: both ramp chain emitters lerped stations
> by CENTERLINE distance; the miter join shortens bend quads' inner
> edges → 4 %-planned descents read 4.3-4.6 % along them.  Stations now
> lerp over effective length (min of centerline/both edges); sloped
> ramp values 0.1→0.01 m.
> **SCOREBOARD at 52fff98**: CYXY 0+320 · SPJC 1+0 (the apron 77 m
> emit-repair divergence — architectural: move to_osm's buffer(0)/
> needle repairs pre-projection like decimation) · SPLP 0 per-tile ·
> HECA 16+11351.  Suite 11F/391P == base11 across the runway-end-skirt
> merge (+56 skirt tests green).  ⚠ /tmp was purged: lab full_build.py
> + base11.txt recreated; old baseline patches gone.
> **HECA BREAK REVIEW (design)**: 14 pockets, med ~2 %, p90 ~2.5 % =
> designed gentle blends; worst spike 255 %/1.4 m (one service_road
> step, probe-worthy).  92 % of broken nodes are pocket INTERIORS —
> anchor attribution needs floor/ceil provenance in feasibility_project
> (proj_lab + solve-state dump are the base).  Decision pending: accept
> ~2 % quarantine ramps vs fund the provenance tool.

---

# STATUS — SESSION 20260705 (part 15c): **SPLP per-tile = 0, CYXY = 0** —
# runway 0.1 m rounding retired + exact clamp-floor geometry (`d6d4284`);
# test break-quarantine (`51dcbf4`)

> **USER FLAGGED SPLP-14 AS SUSPECT — CONFIRMED, two real causes**:
> (1) the runway family still emitted on the LEGACY 0.1 m grid (20 sites:
> redistribute/regrade/runway_segments/tile_cut/seam_anchors) — ±5 cm per
> endpoint = the whole 1.55-1.57 % class; all → 0.01 m.  (2)
> runway_clamp_floor guaranteed pins vs the NEAREST axis point only (L1
> vs L2 gap → lawful-floored pin 2.17 % from a runway-edge weld, both-
> hard, unfixable); floor now = max over axis samples of profile(t) −
> cap·distance(P, cross_section(t)) with half-width credit (persisted
> per-profile pre-cut, cross-tile deterministic).
> **SPLP SCOREBOARD NOTE**: measure SPLP PER-TILE (production path) —
> the whole-airport lab build pins seams through a writer production
> never uses.  Per-tile: 0 within both tiles.
> **KDFW quiet re-measure (task 4b)**: 840.5 s at HEAD~ (solve 362.7,
> final projection 21.1 s @ 22,740 nodes decimation-first), 41
> actionable + 0 break, apt_mtime 1783220791.  NOT comparable to the
> old 529 s (many feature commits between); no post-build hang
> (watchdog clean).
> **SCOREBOARD at d6d4284 (matching apt_mtimes)**: CYXY **0**+320 ·
> SPJC 5+0 · SPLP **0** per-tile (2 cross 9 cm hairlines newly EXPOSED
> by honest rounding; profile 1.61 % pre-existing) · HECA 17+11356 ·
> KDFW 41+0.  Suite 11F/335P == base11; fast lane 7F.
> **REMAINING CLASSES**: SPJC 3 tunnel_ramp (curved ramp chord-vs-arc
> — ramp law anisotropy) + 2 apron small-excess; SPLP profile
> anchors-as-floors reconciliation (longitudinal 1.61 %) + seam-cut
> 0.85 m hairline corners; HECA 17 + break-region design review
> (sampler script in session scratchpad); smoothing-aware lazy
> certificates (soundness analysis first).

---

# STATUS — SESSION 20260705 (part 15b): **CYXY = 0 ACTIONABLE** — emit
# decimation moved BEFORE final projection (`8ca25a3`)

> **THE RESIDUAL JUNCTION CLASS WAS DECIMATION-MINTED MESH**: emit
> decimation ran AFTER final_grade_projection; removing ~7k boundary
> vertices re-triangulates junction interiors, so the decimated ring's
> MESH holds chords the projection never enforced (probe: every residual
> pair present in a fresh joint at the validator's own budget,
> seed-violated, endpoints free).  The old docstring claim "removed
> vertices only remove already-satisfied pairs" is FALSE for the mesh.
> **FIX (`8ca25a3`)**: decimation → final projection (now truly the last
> word on the rendered geometry); geom_guard stays pre-decimation;
> final projection's OWN broken pockets now exported to break_nodes
> (they previously leaked into the actionable count).  BONUS: the
> projection runs on the decimated node set — SPJC 8243→4207 nodes,
> 8.5→3.4 s, converges 0 over-cap (KDFW should benefit more — re-measure
> queued).
> **SCOREBOARD (matching apt_mtimes)**: CYXY **0**+320 · SPJC 5+0
> (3 tunnel_ramp over their 4 % cap + 2 apron small-excess) · SPLP
> 14+36 · HECA 20+11205.  Session start was 28/56/15/104.  Suite
> 12F/334P == base12 (both commits).
> **NEXT**: test_pavement_grade should consume break_nodes like the CLI
> (CYXY test would go green at 0 actionable); SPLP-14 composition; SPJC
> tunnel_ramp 4.3-4.6 % class; HECA break-region design review at 11 k
> scale; KDFW quiet re-measure (decimation-first projection win).

---

# STATUS — SESSION 20260705 (part 15): cm-noise class CLOSED — exact-mesh
# sidecar (`f1392e9`) + LAW-GUARDED post-projection fairing (`665597c`)

> **MESH-DRIFT PREMISE REFUTED, REAL CAUSE FOUND**: the SPJC 43-pair
> cm-noise class was NOT solver-ring vs emitted-ring Delaunay drift —
> forensics (scratchpad mesh_pair_forensics.py) showed 43/44 pairs
> violated at FULL PRECISION in-memory (in-mem de == emitted de).  Root
> cause: `final_grade_projection`'s `_fair_ring_edges` call runs AFTER
> the last feasibility projection with nothing re-enforcing the pairs it
> perturbs; junction MESH chords (crossing between ring runs, invisible
> to the ring triples) got pushed a median 1.8 cm over.  A/B
> O4_EDGE_FAIRING=0: SPJC 57→29.
> **FIX (`665597c`)**: fairing moves clamp into the node's law-edge
> interval (one_solve._build_adjacency over `joint`, margined budgets);
> already-outside/infeasible ⇒ never move; never-expanded lazy shapes'
> nodes anchored.  Solve-time call stays unguarded (projection re-enforces).
> **EXACT-MESH SIDECAR (`f1392e9`)**: sidecar "mesh_edges" = solver's
> junction mesh 1:1 (grade_graph.MeshEdgesExact, SHARED_VERTEX_TOL_M
> match); build byte-identical; honest +1 at SPJC (emitted-ring Delaunay
> had hidden a real pair).
> **SCOREBOARD (matching apt_mtimes)**: CYXY 28→14 (+breaks 218→222),
> SPJC 56→24, SPLP 15→15 (37→40), HECA 104→66 (9527→9555).  Suite
> 12F/334P == base12.
> **NEXT — residual SPJC junction class (18)**: 1.54–1.79 % on 28–175 m
> chords at flat budgets = pairs the projection never saw; suspect emit
> DECIMATION (7,115 collinear verts removed ±0.02 m) minting long
> ring-adjacent edges spanning many solver segments.
> ⚠ MACHINE: /usr/bin/git hits an unaccepted Xcode license (new Xcode);
> use /Library/Developer/CommandLineTools/usr/bin/git or have the user
> run `sudo xcodebuild -license accept`.

---

# STATUS — 20260705 ADDENDUM: the "build-concurrency corruption" was a
# LIVE INPUT — the user's Custom Scenery CYXY apt.dat was being edited
# between measurement windows (o4_apt_dat_mtime provenance proves it:
# 3 mtimes = the 251/176/257 count eras exactly).  Builds deterministic
# given inputs.  PROTOCOL: verify o4_apt_dat_mtime matches across any
# compared patches (full_build.py prints it now).  Full-width service
# corridor rule SHIPPED (68e77d9, user ruling): half-strips consolidate
# pre-solve, conversions span the spine — CYXY 28+218, SPJC 56+0,
# SPLP 15+37 at apt_mtime 1783275372.

# STATUS — SESSION 20260705 (part 14): tests realigned 21F→12F
# (`56e19fd`); sparse tessellation verdict (`df15809`); py3.13 + fast
# lane (`e06498e`); scoped final projection (`370b0ed`)

> **CYXY "underpass regression" RESOLVED = MEASUREMENT ANOMALY**: full
> bisect 8495328→0e425c1 all = 176; CYXY emits ZERO tunnel shapes; the
> 169/251 readings came from two irreproducible windows (concurrent-
> build suspect; incident + protocol in memory nondeterminism-cause).
> True chain: 176 flat → 152 at the quant margin.
> **TESTS (`56e19fd`, agent)**: 21F→13F; instruments UNIFIED —
> test_pavement_grade now consumes taxi_axes_exact_ll + anchor + seam
> pins and agrees BYTE-EXACTLY with check_grade everywhere (HECA true
> baseline = 9,739); same_nodes rewritten to the approved invariant
> (emitted verts ⊆ final-projection graph, GREEN); compare-targets
> recut; vacuous junction test deleted; CYXY terrain-following
> threshold re-derived from the reach band (710.1 vs permitted 714 —
> honestly red).
> **SPARSE TESSELLATION (`df15809`)**: adaptive bezier (sagitta 0.4 m
> + 4 m spacing floor) + Douglas-Peucker source-ring resampling,
> O4_ADAPTIVE_BEZIER.  VERDICT: node-count lever REFUTED at fixtures
> (HECA 8800→8800 — density is minted DOWNSTREAM by slice/junction/
> welds); kept for the real wins: SPLP off-source class CLEARED
> (rests_on_source[SPLP] green) + SPLP 179→52.  Suite 12F/334P
> (/tmp/base12.txt).  Counts now CYXY 164 / SPJC 66 / SPLP 52.
> **PY3.13 + FAST LANE (`e06498e`)**: 3.13 verified compatible
> (wheels ✓, counts equivalent, ~5-10 % faster — C libs dominate);
> RECOMMENDED NOT REQUIRED (floor stays 3.11 via numpy 2.4; installers
> float; ONBOARDING documents).  tools/fast_suite.sh = cheap-airport
> suite 83 s vs 208 s (full suite stays the merge gate).
> **SCOPED FINAL PROJECTION (`370b0ed`, agent + integrator gates)**:
> law graph rebuilt only for post-solve geometry/value-changed shapes
> (writeback + fairing snapshots, shared-vertex aware).  Gate-off
> byte-identical; counts exact; suite identical.  KDFW timing pending.
> **OPEN**: (1) smoothing-aware lazy certificates — agent ran out of
> credits; needs the soundness analysis (clamps/anchors vs certified
> interiors) first; certificates currently all expand.  (2) KDFW
> timing re-measure with scoped projection.  (3) DRIVE-TO-ZERO
> campaign (class plan + per-violation JSON in session scratchpad):
> SPLP-52 break-region tagging, CYXY hillside corridor blend, SPJC
> junction #158 probe, shared-corner wobble co-location, CYXY building
> pad tilt ×2; HECA 9,624 campaign after.

---

# STATUS — SESSION 20260704 (part 13): service 5% + break blends +
# learned ETA (`8495328`); tunnel refactor (`d85db1d`); KDFW underpass
# corridors (`14f1da4`); perf round IN FLIGHT

> **SERVICE ROADS (user)**: SERVICE_ROAD_MAX_GRADE 4→5 %;
> apply_service_road_dem_follow floor>ceiling contradictions now fill
> with the distance-weighted break blend (was: silent ceiling clamp =
> wall at the groundside mouth).  ⚠ the first cut of the reach walk
> HUNG CYXY 27 min (epsilon-tolerant pop guard + lazy pushes re-expand
> equal-value duplicates — parallel merged legs have many equal paths);
> fixed = strict `if k in best` guard, memory file written.  A/B:
> CYXY 180→169, SPJC 107→104 (the "67" note was stale), suite 21F/325P
> identical list.
> **ETA (user: KDFW stuck at "About 0:06")**: the monotone min-clamp
> LOCKED an early optimistic guess.  New `build_time_model`: every full
> build records complexity features + per-phase/total wall times
> (~/.ortho4xp/auto_patch_build_times/); rebuilds predict from own
> history, first builds from a cross-airport per-complexity rate;
> BuildProgress refines per phase (finished phases replace predictions,
> rest rescaled by ahead/behind ratio, confidence-weighted); GUI blends
> prior with elapsed extrapolation (quadratic weight) and the display
> may now RISE past a hysteresis band (10 s / 15 %).  KDFW recorded:
> 668 s (solve 457, emit 143), 1,519 taxi edges — next rebuild shows a
> calibrated ~11 min from the first seconds.
> **TUNNEL REFACTOR (`d85db1d`, user: "excessively large — audit")**:
> _emit_tunnel_portals (2,100 lines) → orchestrator + 14 stage helpers,
> byte-identical at SPJC/KCLT/CYUL.  Audit list (numbered import
> aliases, fork-throat off-paths, duplicated projection helpers, dead
> params, stale comments) in the session transcript — cleanup queued.
> **KDFW UNDERPASSES (`14f1da4`, four user rulings)**: motorway 25 m /
> secondary 15 m; <35 m grouping = ONE corridor ramp
> (UNDERPASS_GROUP_DIST_M; KDFW motorway pair at ~113 m stays separate);
> ALL breaks pavement-derived at taxi edge +1 m (mapped tunnels re-split,
> O4_TUNNEL_TAXI_BREAKS; wall cap = [edge, edge+1 m]); building/apron-
> covered mapped tunnels → building_passage (no ramps), grass/RESA ones
> KEEP mapped portals (CYUL regression caught + fixed); gaps too short
> for a ramp pair (<2·depth/grade) merge bores + emit corridor-width
> flat rect at −8 m with DEM-following wall band
> (O4_TUNNEL_LOW_CONNECTORS); portals inside corridors suppressed;
> fork branches >50 % throat-covered skipped.  KDFW 546→447 law-true
> (facing-ramp 82 % overlap class GONE, ramp overlaps 9→0, bore ends
> exactly 1.0 m from taxi edges), SPJC 104→96 (4 clusters kept — user
> should eyeball Elmer Faucett in sim: ramps 82→39 + 1 flat connector),
> KCLT 174 unchanged, CYUL restored 5 clusters/29.  Suite 21F/325P
> identical list.  Offline iteration: O4_DUMP_PRE_TUNNEL_LAYOUT pkl +
> /tmp/spjc_lab/tunnel_replay.py (seconds per iteration).
> **PERF ROUND SHIPPED (b783501 + 0e425c1): KDFW 668 → 529 s (−21 %)**.
> Stack verified native (arm64 python, Accelerate-BLAS numpy 2.4.3,
> GEOS 3.13; M5 Max 6P+12E).  KDFW relief 37.7 m/7.8 km = 0.485 % avg.
> cProfile-driven: (1) grid-bucket `_enforce_shared_vertices` (the
> O(n²) "n is typically 200-2000" scan hit 47k verts = 1.1e9 pairs ×2);
> (2) ANCHOR COLLAPSE in reach_band_unified + _build_skeleton_band —
> per-anchor cap-Dijkstras (550 at KDFW, 140 s) → 2 value-seeded
> multi-source fields (min(ae+dist)/max(ae−dist) commute; fields carry
> (dist, ae) so floats form with the original association).  Both
> BYTE-IDENTICAL at SPJC/CYUL/KDFW.  (3) WORKLIST Gauss-Seidel in
> feasibility_project scalar path (FIFO violated-edge queue,
> deterministic, visit cap = old bound): solve 393→326 s; different
> legal fixpoint — CYXY 251→251, SPJC 99→97, KDFW 447→351, suite
> identical.  NEXT PERF TIER (user wants ~5×): flatness-gated
> CONSTRUCTION skipping — per-shape conservative envelope BEFORE pair
> generation (shape_constraints 108 s instr., clearance 144 s,
> final_grade_projection rebuild 89 s); geometry/emit (~210 s real) has
> its own ordinary queue.
> **QUANTIZATION MARGIN SHIPPED (ca97485, agent-implemented)**:
> EMIT_QUANTIZATION_MARGIN_M = 0.01 (env O4_QUANT_MARGIN) — sweeps/
> envelope/break detection enforce budget−1 cm at feasibility_project's
> edge_lim choke point, tally reports vs RAW law, floor 5 mm (0-budget
> flat-cross edges untouched).  The emit-rounding hairline class
> collapses: CYXY 251→152, SPJC 97→61, SPLP 193→179 (seam pockets fine
> — blend lands on the raw-cap ramp).  Suite 21F/325P identical.
> **OPEN**: CYXY 169→251 (+82) came from the UNDERPASS commit
> (discovered in the worklist A/B; margin now masks it at 152 — still
> uninvestigated: which CYXY ways now synthesize bores; check
> _IMPLIED_MAPPED_NEAR_M 40→6 admits; consider a tunnel-count
> acceptance test).  SPJC 96→99 shift came from the CYUL grass-fix
> branch refinement (built-over retag now requires building/apron
> cover).  Tunnel cleanup landed byte-identical (42bf218, −47 lines);
> deferred: fork-throat off-paths, _is_new_cand class, walk-logic dedup.

---

# STATUS — SESSION 20260704 (part 12): monotone ETA (`0055c85`); CYXY
# turnaround pad (`c5f5a2d`); SPJC production tunnels = rebuild needed

> **ETA (user)**: EMA of the total-time estimate (α .15, +10 % margin);
> DISPLAYED remaining is monotone non-increasing (counts down with the
> clock, drops on improvement, FREEZES on stalls — never rises).
> **CYXY #56 turnaround pad**: cover 1.00 but run 14 m < min_run 30 —
> a fully-corridor-contained piece (cover ≥.95, run ≥5 m) now converts
> regardless of run: pad at DEM 706, road descends the straight to the
> flat roundabout 705.3-706.1, cliff gone.  CYXY 180 law-true
> (≥5 % = 0, steps/cross/overlap 0).
> **SPJC user patch (17:59)** predates f7bb741 — production tile path
> verified at HEAD: 4 clusters / 41 ramps incl. both terminal bores.
> Rebuild the tile.
> **CYXY remaining 180 (drive-to-zero queue)**: 126 sub-0.5 % excess
> hairlines + 38 sub-1 % (2-decimal rounding on 1-4 m chords — |Δe|
> 0.05-0.18 m; worst 6.18 % over 2.1 m) + 16 pairs 1-5 % (service-road
> DEM-follow tails: #40's 4.8 % over 26-40 m at the mouth ramp class).
> Next levers: rounding-noise allowance on sub-4 m chords OR 3-decimal
> groundside/service emit; service mouth-ramp law treatment.
> Suite 21F/325P identical.

---

# STATUS — SESSION 20260704 (part 11): KDFW tunnels FIXED (`f7bb741`)
# — ROLE_BOUNDARY portal gate retired (sparse-ribbon false veto)

> **KDFW zero tunnels (user)**: 19 implied bores formed, ALL passed the
> adjacent-road system veto — then the SILENT boundary-distance portal
> gate dropped all 38 portals: since the at-DEM ribbon skip
> (2026-07-03) only 3 ribbon scraps survive at KDFW, all >1 km from the
> central corridor.  Final-layout replays MASKED it (post-tunnel
> boundary→DEM bridges land near the corridor and satisfy the gate) —
> order-dependent state; the O4_DUMP_PRE_TUNNEL_LAYOUT mid-finalize
> dump reproduced it offline.  FIX: boundary gate retired; the
> airside-PAVEMENT distance gate now covers every candidate class.
> KDFW 0 → 14 portal clusters / 151 ramps; SPJC unchanged (4/41).
> Also: finalize no longer swallows tunnel-emit failures (loud WARN);
> per-portal drop reasons under O4_TUNNEL_DEBUG.
> Suite 21F/325P identical.

---

# STATUS — SESSION 20260704 (part 10): loop-route merge FIXED
# (`41e2fa8`); progress window rework (`514dfd5`); honest solver banner

> **LOOP-ROUTE MERGE BUG (user: hump still there after rebuild)**: for
> a LOOP route, plain project() onto its own line returns the vertex
> itself (distance 0) — the out-and-back legs NEVER merged (only
> cross-route pairs did; the part-8 verification hit one of those).
> `_project_excluding` splits the line at arc±60 m and projects onto
> the remainders → the opposite leg.  Verified at the user's point
> (60.7095257,-135.0734434): legs coincide within 0.5 m, cross-section
> flat 703.12 across ±12 m.  CYXY merged runs 2→3, law-true 180.
> NO separate worktree was ever involved — the user's rebuild had
> simply picked up the mid-fix state.
> **PROGRESS WINDOW (user spec, `514dfd5`)**: finished rows LEAVE the
> list (shrinks as the tile completes; fails stay red; window closes
> when the last row leaves); detail centered under the bar, [x/x]
> numbering dropped; per-row timers — elapsed left, "About m:ss
> remaining" right (elapsed × remaining fraction, "estimating…" <3 %);
> window 470→560 wide.  Smoke-tested headed.
> **SOLVER BANNER (`d799d03`)**: "per-surface Jacobi converged in
> N/N iters" passed the FREE-NODE count as both numbers — NOT an
> iteration cap (part-9 perf note corrected).  Active solve = ONE
> route-profile solve on the single unified graph; "per-surface" is
> only the package name now.
> Suite at `41e2fa8` 21F/325P identical.

---

# STATUS — SESSION 20260704 (part 9): fairing precompute (`a7c5848`);
# chord-fit REJECTED; SPJC tunnels NOT reproducible; perf audit

> **Chord fit (user suggestion) MEASURED + REJECTED**: assigning each
> straight run the chord between endpoint values (band-projected,
> 0.15-0.3 m move guard) raised CYXY within-shape 182→237-256 with no
> visible gain — band clamps + cross-run pairs make the chord
> not-quite-feasible; POCS-from-seed already converges in a few sweeps.
> Kept: triples PRECOMPUTED once (per-sweep geometry work eliminated,
> fairing runs 2×/build).
> **SPJC "tunnels 4→2" (user)**: NOT reproducible at HEAD standalone —
> 4 portal clusters emitted, both ~1270 m twin terminal bores present
> (7+13 tunnel_ramp shapes).  Twin-rail suppression only touches
> railway ways (SPJC bores are highway).  Likely stale Ortho4XP module
> cache (RESTART Ortho4XP — the standing gotcha) or a mid-session
> build.  If fresh production tile still shows 2: get the tile build
> log with O4_TUNNEL_DEBUG=1.
> **PERF (user: tile creation slowed)**: standalone SPJC 68.3 s vs
> ~80 s at session start (net FASTER).  Session adds:
> final_grade_projection ON = 7-22 s/airport (SPJC 7.4, CYUL 22.1) —
> the one real new cost, buys the post-solve mutation-class closure
> (CYXY 299→97 back then); edge fairing + corridor/lens/merge passes
> <1 s each.  Remaining big line items (pre-existing): per-surface
> solver hits its 3019-iter cap at SPJC (29.5 s), final projection's
> full law-graph rebuild on final geometry (can't reuse solve ctx —
> node indices differ).  NEXT perf levers if wanted: scope final
> projection to post-solve-CHANGED shapes (geom_guard tokens), solver
> iteration-cap convergence.
> Suite 21F/325P identical.

---

# STATUS — SESSION 20260704 (part 8): CYXY ridge + waviness CLOSED
# (`c220e66`) — parallel truck legs merge; airside ring-edge fairing

> **RIDGE (user)**: two-lane road = two one-way truck routes (CYXY
> 'Crew cars' is one out-and-back LOOP) → a spine per leg → two
> profiles meeting at a center ridge.
> `apt_dat_reader.snap_parallel_service_runs` (gate
> O4_MERGE_PARALLEL_SVC): parallel runs ≤9 m for ≥20 m → first line
> deforms to the MIDLINE, second's run replaced by the exact SUBSTRING
> of the first (identical geometry until divergence) → one spine down
> the middle (user ruling).  Cross-section now flat, ridge gone.
> **WAVINESS (user, taxiway E edge)**: ring EDGES aren't spine chains —
> the fairing law never covered them; the GS distributes a cap-grade
> climb as a ±0.8 % sawtooth every 12 m.  `_fair_ring_edges` = the
> second-difference POCS on STRAIGHT boundary runs (bend-tested;
> anchors fixed; band-clamped), at solve end AND after
> final_grade_projection (which re-perturbed it).  Service/groundside
> EXCLUDED (fairing their mouth-weld ramps minted 0.9 m bumps,
> measured).  Gate O4_EDGE_FAIRING.  E now emits long smooth segments.
> CYXY law-true 182 (≥5 % = 0, steps/cross 0): fairing converts hidden
> below-cap sawtooth pairs into honest sub-0.5 % hairlines on cap-grade
> climbs (127/182) — long gentle slopes win per the standing ruling.
> Suite 21F/325P identical.

---

# STATUS — SESSION 20260704 (part 7): SPLP seam edge anchors + ramp-start
# trim + spike cleaner + twin-rail bores (`246384f`)

> **SPLP runway west seam (user)**: Ortho4XP preserve_boundary pins the
> tile LINE to raw HGT; the profile anchored only at the CENTERLINE
> crossing (whose alt_strict sample often fails at the tile's own edge)
> → west edge contact 2.5 m under the render line.
> `redistribute_runway_profile` now anchors at the runway EDGE
> crossings (hump-class only — a ravine-side anchor measured −2 m drag
> on neighbouring interior samples → taxi stub ceiling fell 0.8).  West
> edge 58.50→60.30 (raw line 61.0); stub band pins now EXACTLY at DEM.
> Gate `O4_RUNWAY_SEAM_EDGE_ANCHORS`.
> **OPEN (stub, user finding 2)**: interior nodes still top at the
> runway-reach band ceiling ~1 m under the seam pins (milder
> rise-then-dip persists).  A node_band override (pin−cap·d floor +
> ceiling raise) measured INEFFECTIVE — solved values ignore it; a
> later pass (phase-A frozen spine suspected) writes 61.46 last.
> Band-vs-pin precedence = its own round; REJECTED so far: band
> override at solve.py level, one_solve-internal floor (phase-A misses
> both).
> **RAMP-START TRIM (user, all airports)**: apt.dat row 1300 parses;
> `taxi_centerlines` drops LEAF chains ≤80 m ending within 30 m of a
> ramp start — CYUL 887→720 pieces (−167 lead-ins).
> **CYUL stray node**: apron #233 carried a 251 m ZERO-AREA out-and-back
> needle (ring visits far point, returns to the same coord) —
> `_dedup_coincident_ring_vertices` now removes spike tips whose
> neighbours coincide.  Node gone.
> **KCLT twin rails**: two parallel railway=rail lines <10 m apart = ONE
> `railway_twin` 14 m bore (user: 12-15 m for two rails); twin's portals
> suppressed.  Ramps now ~16-17 m chains, portals 0.5-1.6 m from the
> taxi edge.
> Suite 21F/325P identical list.

---

# STATUS — SESSION 20260704 (part 6): CYXY findings 1-3 CLOSED
# (`cc5a4ad`) — lots at DEM, corridors as roads, final projection ON

> **User findings**: (1) lot #35 at apron level + road #40 no rise;
> (2) taxiway G 3 % allowance suspect; (3) #206 groundside but rides a
> truck route; (4) then drive CYXY within-shape to zero.
> **CYXY law-true 414 → 97** (residual = sub-1 % hairline: 72 pairs
> <0.5 % excess, worst 6.07 % on a 0.10 m rounding chord), steps/cross/
> mid-edge/self-overlap 0.  SPJC 72 → 67 (stash-A/B: the 72+15-step
> baseline is PRE-EXISTING at part-5 HEAD, the "53" note was stale).
> (1) **MOUTH-DECAY relevel**: the reach's uniform shift sank the 12 k
> lot 3.8 m under terrain (53 m route × 4 % can't span the rise).  Now
> each node takes the mouth's delta decayed at cap/metre from the
> nearest mouth — mouth meets road exactly, interior at DEM (+0.00).
> (2) **G is law-true**: code-A segments earn 3.0 %; the ceiling comes
> from the code-D feeder (1.5 % per ICAO) + runway anchor 694.3 →
> ~705.2 vs DEM 711.9.  Verified by Dijkstra over the dumped spine_adj
> (O4_DUMP_SOLVE_STATE now includes spine_adj + runway_anchor).
> Buildings 5/7 seat off the same band — correct.
> (3) **reclassify_groundside_route_corridors**: OSM groundside riding
> a truck route ≥30 m at ≥70 % corridor cover → service_road pre-solve
> (route N's 835 m corridor was rigid-shifted −9 m; now grades axially
> and REACHES DEM).  Converted pieces trim against existing pavement;
> new last-word `_deconflict_service_overlaps` (before the final
> T-weld) clips the canonical-weld lens class (0.38 m²) with
> projection-inserts (no residual T-junction).
> **Lockstep fixes en route (one field, one writer)**: solve-time chord
> limit on re-levelled lots BEFORE welds read them; welds = the ONLY
> reach truth-pins (RAISE writes seeds, hard-pinning froze arm nodes
> 1.3 m under welded mouths → 61 % chords); pavement-node weld (mouth
> vertex often lives on the APRON arm — svc-ring weld missed it), keys
> persisted for the post-solve limiter to re-adopt; post-solve
> separations PRESERVE the altitude field of clipped pieces (raw-DEM
> resets detached welded roads by 5 m); groundside rounding 0.1→0.01 m
> (the V15 stairs class); **final_grade_projection DEFAULT ON** (the
> "no change" verdict predated the exact-axes sidecar; closes the
> post-solve mutation classes, CYXY 299→97, SPJC −5).
> Suite 21F/325P identical list.  NEXT (task 4 continues): the CYXY
> sub-1 % tail (95 pairs — service DEM-follow noise + rounding on
> sub-metre chords), the SPJC 67 + 15 pre-existing steps
> (building16↔building30 1.95 m @0.6 m), HECA.

---

# STATUS — SESSION 20260704 (part 5): P4 CLOSED (`468a7c6`) — route-END
# mouth edges kept + flush groundside merge

> **P4 CLOSED (user directive: teach the separation to keep the shared
> edge wherever the abutting pavement carries a truck-route END)** —
> two mechanisms, both in groundside.py:
> 1. `_separate_groundside_from_airside`: apron/junction pavement
>    carrying a truck-route END (≤1 m) joins the clip UNBUFFERED inside
>    a 15 m square mouth window around the end (clearance buffer
>    subtracted there; overlap still trimmed).  Gate
>    O4_GROUNDSIDE_ROUTE_END_EDGE default ON.  Fixed a sibling instance
>    outright: 165 m² demoted connector exactly 1.00 m
>    (= GROUNDSIDE_CLEARANCE_M) from its 6,776 m² lot — the source of
>    CYXY's worst violations (service roads spanning a 9 m cliff, 740 %).
> 2. THE P4 RESIDUAL WAS ONE LAYER DEEPER: connector #76 and the
>    49.5k m² lot were already FLUSH along ~13 m, but
>    `_merge_touching_groundside` measured shared boundary by EXACT
>    ring∩ring length ≈ 0 on mm-offset runs → merge refused →
>    independent DEM-follow/shift left coincident nodes 2.6 m apart
>    (the status-line "2.6 m apart" was ELEVATION).  Now: shared
>    boundary = run of one ring within touch_tol of the other, and
>    group members SNAP onto the accumulated union pre-union so the
>    hairline dissolves.
> CYXY law-true A/B: within-shape 414→103 (rest = pre-existing
> sub-metre hairline tail), cross-shape 5→0, steps 10→0, mid-edge
> 35→0, coincident-node groundside mismatches 4→0, groundside pieces
> 15→11 (connector+lot complexes = single surfaces).  Suite 21F/325P
> failure list IDENTICAL to baseline.

---

# STATUS — SESSION 20260704 (part 4): CYXY dropped intersections + CYUL
# flipped wall FIXED (`602264b`)

> **CYXY dropped taxi-intersection pieces (user, 3 coords)**: coverage
> probe named `ce-post-runway-clip` — the runway clip drops remainders
> <50 m²; the strip carve shrank parent junctions so real 20-50 m²
> intersection remainders fell under the floor.  Now compact small
> pieces KEEP (≥4 m² + survives buffer(−1)); hairline slivers still
> drop.  Restoring them exposed a carve defect: mutually-overlapping
> post-slice faces emitted the same corridor area as service (face A)
> AND kept it as apron (face B) — carve now subtracts the FULL corridor
> from every remainder + dedupes emitted pieces.
> **CYUL east tunnel wall flipped (user)**: the perimeter band annulus
> crosses the road at BOTH ends; the hole-slit knife cut the band at
> its NARROWEST point = the true portal cap → only the far-end crossing
> survived (wall across the live road).  Band now cut OPEN at every
> arm's far end (also makes it simply connected → the cap survives).
> Crossings: portal-only ✓; SPJC tunnels byte-stable.
> Suite 21F/325P identical.
> **P4 GROUNDWORK (093a1e7, USER RULING: connection identified EARLY,
> lot classified BY its service-road connection, gap never cut)**:
> conform_service_mouths_to_groundside (shared vertices into lot rings
> at service mouths) + route-END mouth welds for apron-unreachable
> connectors + largest-lot key preference + pre-solve groundside merge.
> Road now welds flush to the demoted connector (698.5 = 698.5 ✓).
> REMAINING at P4: connector piece ↔ LOT still two groundside surfaces
> 2.6 m apart — the 1 m clearance gap was cut while the connector
> pavement was still AIRSIDE vs the lot; demotion doesn't re-close it,
> so the pre-solve merge sees disjoint pieces.  NEXT: bridge the
> historical gap at demotion (extend the demoted piece to the lot
> across ≤ GROUNDSIDE_CLEARANCE_M), or teach the separation to keep
> the shared edge where the abutting pavement carries a truck-route
> END (the "identify the connection first" ordering, fully realized).

---

# STATUS — SESSION 20260704 (part 3): implied tunnels @KCLT; SPLP seam
# tension NAMED; CYXY centered service strips (`1b94fba` `48ef440`)

> **IMPLIED TUNNELS SHIPPED (1b94fba)**: unmarked road/rail crossing
> taxi/runway pavement ⇒ synthetic tunnel=yes bore split at the
> pavement-edge crossings; whole portal machinery applies.  KCLT (user
> test): twin-track rail detected under TWO taxiways (5 bores, 33 m) →
> 4 portal clusters; delta 100 % inside tunnel_ramp shapes.  Gate
> O4_IMPLIED_TUNNELS.  Inert at all other fixtures.
> **SPLP SEAM TENSION (analysis, no code)**: ALL 225 broken nodes share
> ONE anchor pair — runway vertex 74.0 (285 m inland, profile-true) vs
> band-edge seam pin C 63.5 (terrain): 10.5 m drop over ~520 m = 2.0 %
> average (6 % at the ravine wall) vs the 1.5 % cap ⇒ 3.84 m deficit.
> Both anchors are "legit" given the emitted footprint BUT (a) the pin
> values are coarse smoothed-SRTM reading the RAVINE at the tile line
> (real pavement there is likely elevated fill the 90 m posts can't
> see), and (b) the pavement REACHING the seam there is largely apron
> #29 = 19 % ON SOURCE (the known rests_on_source over-emission,
> junction #24 at 99 % also touches).  Levers: rests_on_source fix
> (queued since V14) shrinks the tension region; the break-blend
> renders what remains as the least-bad contained ramp.
> **CYXY CENTERED SERVICE STRIPS (48ef440)**: all four user rulings
> measured green (P1 pad → groundside; old-31 → groundside; the 5-7 m
> narrow strip → service_road whole-width; road end touches groundside
> 542).  carve_narrow_service_strips + traversable-edge chain rule
> (≥1 m) + apron-lot demotion (truck-through skip now junction-only) +
> final scoped sweep + last separation.  Conformance WARN gone;
> within-shape 94→72.  OPEN: P4 road mouth emits 3.1 m below the lot —
> the mouth lands MID-EDGE on the lot ring so the key-based groundside
> mouth weld can't bind (needs edge-interpolated weld; blanket pinning
> measured +215).

---

# STATUS — SESSION 20260704 (part 2): seam-as-anchor ruling + tasks 3/4/6
# CLOSED (`5c23ff1` `f559ae5` `3997755` + coverage tool + `3a3dfd7`)

> **USER RULING**: the seam is a hard anchor the solver GRADES to (like a
> runway edge or building) — smooth, seamless transition.
> **SEAM-AS-ANCHOR (5c23ff1)**: the reported bump (-12.1592847,-76.999938)
> was a seam pin trampled twice — apron SEAT stamped over the pin (63.5→
> 66.3) then O4_YIELD_FREE_APRON_SEATS freed it for the final GS.  Now:
> seam pins are NEVER seats, never in movable pad groups, always re-added
> to yield_hard.  ONE seam definition in both readers (solver had NONE —
> build_context never set seam_keys; validator blanket-exempted a 400 m
> ZONE): ctx.seam_keys = the published pin set (layout._seam_pin_idx);
> sidecar exports seam_pins; check_grade flags only pin-coincident nids.
> grade_law: one-seam pairs never earn spine/blend credit (body cap).
> Pin-pair projection couples consecutive pins along ring PATHS + across
> shapes along each band edge.
> **BREAK CONTAINMENT (f559ae5)**: the honest pin-based validator exposed
> a GENUINELY infeasible pocket (seam terrain 62-66 vs runway-held plateau
> 70-72 over too little path).  The final GS cycled POCS on it → ±1 m
> noise at 10-14 %.  feasibility_project now detects break regions
> (reach-envelope floor>ceiling), freezes them out of the sweeps, and
> fills them with the DISTANCE-WEIGHTED BLEND t=d_ceil/(d_ceil+d_floor),
> z=hi+(lo−hi)·t — ON the pin-descent field at the seam, ON the floor
> field at the high anchors, continuous at the region boundary, deficit
> spread as a gentle over-cap ramp.  REJECTED (measured): plain midpoint
> (parks half the deficit as a 1.9 m/34 % wall AT the pin).  Also:
> ring-adjacent pairs are never crosses-spine-skipped (a ring edge is
> physical pavement).  Worst seam-approach pair 36 %/1.9 m → 5.1 %/0.58 m;
> at the user's point only a 2.2 % ramp over 36 m remains.
> **TASK 3 FAIRING (3997755)**: TAXIWAY_MAX_GRADE_CHANGE_PER_M (1/3000,
> tunable O4_TAXIWAY_CURVE_RUN_M) is now the spine-profile vertical-curve
> LAW: _fair_spine_chains POCS on second differences along degree-2 spine
> chains (sag lifts, crest lowers, band-clamped, anchors fixed), gate
> O4_SPINE_FAIRING default ON; check_grade validates the same rate along
> sidecar axes (noise-aware).  SPJC 30 solver-residual triples / 48
> validator kinks (calibration baseline).
> **TASK 4 (coverage tool commit)**: service-road corridors measured green
> at CYXY — 30/30 truck routes covered (tools/check_connector_coverage.py
> = the severed-connector detector), 0 steps at service↔groundside
> boundaries, road-cap 4 % spines.  If the user still sees defects,
> concrete coordinates needed.
> **TASK 6 (3a3dfd7)**: CYUL runway-24-end underpass emitted — divided
> highways SELF-VETOED (each twin bore blocked by the other's surface
> continuation).  Twin-bore exemption (non-crossing + shares a node with
> any tunnel way) + SYSTEM-level veto propagation (union-find by
> proximity; any crossing vetoes the whole system) + walk dedup (4 m).
> CYUL 2 underpasses, LMML emits its genuine Luqa runway underpass
> (tunnel_ramp "steps" there = design ramp↔wall faces), SPJC identical,
> SPLP 0 emitted (20 skipped).  O4_TUNNEL_DEBUG=1 prints verdicts.
> SUITE after all: 21F/325P — identical failure list to session baseline
> (every commit A/B'd).  SPJC 51-55 law-true (hairline wobble, 53 at
> HEAD); SPJC seam-free → seam changes inert there.
> NOTE for next session: the fairing + honest seam validator open two
> drive-to-zero queues (SPJC 48 kinks; SPLP 225 seam-ramp flags = mostly
> the honest <1 %-excess over-cap ramp of the infeasible pocket).

---

# STATUS — SESSION 20260704: task 5 (SPLP seam dips) CLOSED (`0a0284d`)

> **DONE 5**: the "still-unidentified path" was the SOLVER's seam
> hard-anchor block (solver_primitives ~1200) — it RE-SAMPLES the smoothed
> DEM per seam vertex and overrides every earlier hard value ("seam wins"),
> which is why clamping the two node_altitudes writers was byte-identical.
> Fix set (one principle: seam pins come from jointly-graded,
> CUT-INDEPENDENT surfaces, never per-vertex terrain reads):
> 1. runway_redistribute persists the gated per-ref profile
>    (`layout._runway_redistributed_profiles`) +
>    `sample_redistributed_profile(x,y)`.
> 2. tile_cut `_pin_runway_piece_to_profile`: cut runway pieces take the
>    profile at EVERY vertex — replaces BOTH the NN-resample (the 4.6 m
>    cross-seam step of 2026-06-20) and the per-vertex DEM pin that fixed
>    it (which carved the ravine into the runway at SPLP's 18° oblique
>    crossing: corners 141 m of station apart pinned 4.2 m apart = 2× cap).
> 3. Solver seam block now 3-phase: runway-owned buckets keep profile
>    hard-anchors (fixed sources); airside pins take runway_clamp_floor;
>    ring-ADJACENT seam-pin pairs (the law-exempt both-hard class) are
>    POCS-projected onto |Δz| ≤ cap·d — fills the mirrored 1.2-1.3 m
>    terrain-trace dips, identity on cap-legal DEM adherence.  Two
>    REJECTED (measured) operators: geometric band-edge chains + one-sided
>    max envelope (9 m walls on hillsides, couples across grass);
>    ring-run depression fill (endpoints never lift; runs of 2 do nothing).
> 4. runway_clamp_floor evaluates the persisted profiles, NEVER surviving
>    shapes — post-cut each tile keeps only its own pieces, so the shape
>    walk gave 65.7 vs 62.4 across the 10 m gap (3.3 m step, caught by
>    test_cross_tile_cut_edge_elevations_consistent).
> MEASURED: dips 7→6; every ≥1 m dip resolved; the remaining 2.3 m runway
> seam sag (was 4.2) = the FAA-GATED OPTIMUM (cap-grade descent to the
> centerline seam anchor — profile can't legally hold 61.7 over a ravine
> whose seam anchor is ~56).  Cross-tile mismatches 0; parity tests pass;
> SPLP law-true unchanged (1 pre-existing hairline, A/B); suite 21F/325P
> IDENTICAL list to baseline (A/B).  Probes: splp_seam_probe.py +
> dip_writer_probe.py in /tmp/spjc_lab.
> NEXT (queue below): task 3 (vertical-curvature fairing law — also the
> RESIDUAL WAVINESS lever), task 4 (service-road corridors), task 6 (CYUL
> tunnels — check the "skipped 17 tunnel(s) adjacent/crossing road" print).

---

# STATUS — SESSION 20260703 (cont.): user's 6-task list — state

> **DONE 1 (7a19216)**: at-DEM boundary ribbon SKIPPED
> (O4_BOUNDARY_SKIP_AT_DEM, ±0.05 m; keep within 30 m of pavement for the
> seam-adoption interface).  SPJC 1,074 rects skipped → patch 477 ways /
> 8,330 verts; SPLP ~488; CYXY 221.  NOTE: unmasked
> test_cyxy_taxi_e_south_apron_follows_terrain — that test's bbox counted
> the DEM-following RIBBON as "pavement"; airside there truly tops at
> 710.7 vs required 714 = the KNOWN "hill aprons flat at band ceiling"
> open item, now honestly red.
> **DONE 2 (7a19216)**: CYXY bridges were computed then 100 % silently
> dropped — containment ∩ boundary returns a GeometryCollection on
> tangency and the geom_type guard rejected it wholesale.  Polygonal-part
> extraction → 4 bridges emitted, valley probe 78/80 covered (was 0/80).
> Probes: valley_probe.py in /tmp/spjc_lab.
> **OPEN 5 (82c2699, deep diagnosis banked)**: SPLP seam dips confirmed —
> 7 nodes, worst 4.2 m runway V-notch + mirrored 1.2-1.3 m junction dips.
> Pin IS hard pre-solve with law edge present; emitted 63.3 = envelope
> MIDPOINT signature (floor>ceiling ⇒ infeasible pin↔runway chain).
> THREE pin writers found; clamping seam_anchors + _terrain_pin_slice_
> nodes left patches BYTE-IDENTICAL → the junction's 62.0 flows through
> a still-unidentified path.  NEXT: altitude-write tracer on the vertex
> bucket at SPLP local (-132.3, 41.2) tile −13/−77 (dip node), then apply
> the runway_clamp_floor rule at THAT writer; runway notch additionally
> needs redistribute_runway_profile to see the tile_cut band-edge pins.
> Landed groundwork (verified non-regressive): one-seam-endpoint pairs
> stay in the law; runway_clamp_floor shared helper (taxi-cap reachable-
> by-construction pins).  splp_seam_probe.py in /tmp/spjc_lab.
> **QUEUED 3**: tunable vertical-curvature law (fairing) — design agreed
> earlier in session (second-difference limit on spine chains, K-factor
> analog, solver+validator shared).
> **QUEUED 4**: service roads as road-cap corridors, airside↔groundside
> connectors never severed, groundside at DEM grading smoothly up (CYXY
> examples).
> **QUEUED 6**: CYUL tunnels — note SPLP log prints "skipped 17 tunnel(s)
> with an adjacent/crossing road (ramps not modelled)" — the CYUL
> runway-24-end tunnel is likely skipped by the same adjacent-road guard;
> check that print in a CYUL build first.

---

# STATUS — RESIDUAL WAVINESS: rounding rejected; decimation band saturates;
# NEXT LEVER = solver-side FAIRING

> USER: small waves/variations that real grading would smooth into long
> gentle slopes; proposed rounding elevations to 0.5/1 m.  REJECTED with
> evidence: quantization creates terraced STAIRS (0.5 m level change over a
> 24 m segment = 2.1 % grade spike at every boundary) — the V15 waviness
> root cause WAS 0.1 m quantization (fixed by 2-decimal emit).
> MEASURED instead: emit-decimation Z band ±0.02 → ±0.10 m
> (O4_DECIMATE_Z_M knob, committed) removes only ~700 more vertices
> (7,781 vs 7,079) — the residual waves live on CURVES and face interiors
> where XY keeps the nodes, out of decimation's reach.  Default stays 2 cm.
> THE REAL FIX (next session): SPINE-PROFILE FAIRING — the law bounds the
> FIRST derivative (grade) but nothing penalizes grade CHANGES, so the
> solve tracks DEM noise in legal ±1.5 % wiggles.  Add a curvature
> (second-difference) objective on spine chains subject to law + anchors
> (the s63 "vertical-curve extrema design" item, never built) — long
> linear/parabolic profiles = real-world grading.  Alternative form:
> post-solve vertical-curve fit per chain + law re-projection.

---

# STATUS — CYUL 15-MIN BUILD: bug-class scaling, FIXED (861 → 217 s; SPJC
# 89 → 69 s)

> USER: CYUL took 15+ min vs SPJC ~80 s but isn't 15× bigger.  CONFIRMED —
> CYUL pavement is only 1.17× SPJC's AREA (3.5 vs 3.0 km²) but its apt.dat
> route network is 5× more FRAGMENTED (902 vs 171 pieces → 2,455 vs 491
> route-arc pieces → 1,324 vs 268 slice faces).  Geometry scales linearly
> (58 vs 13 s); the ELEVATION phase was superlinear.  cProfile (1,158 s
> instrumented) named two hot spots, both fragment-count-driven:
> 1. **reach-band visible-chord walk** (60 % of the build): ~54 failing
>    candidates per node × 23k nodes, each paying an exact line∩polygon
>    overlay (1.27 M calls, 650 s) in `_nearest_visible_centerline` /
>    `_chord_on_pavement`.  FIX: `_paved_frac` — VECTORIZED point sampling
>    (`shapely.prepare` + `contains_xy`, one C call per chord).  ⚠ a
>    per-point Python-shapely sampler is NOT faster (call overhead ≈
>    overlay cost — measured 861 s, no win); the batch call is the win.
> 2. **`_build_global_spine`**: naive centerlines × nodes = 52 M `_project`
>    calls / 140 s.  FIX: node STRtree + tolerance-inflated bbox prefilter
>    per centerline.
> Output byte-comparable quality: CYUL 7 within-shape / 0 steps (identical
> pre/post fix); SPJC 53 (±1 borderline visibility flip from sampling).
> The final scalar GS was NOT the problem (29 s).  NEXT PERF LEVER if
> needed: line-centric bulk node→centerline binding (corridor nodes bind
> to their own line trivially; only apron interiors need the walk).

---

# STATUS — EMIT DECIMATION SHIPPED (user design): node density now follows
# the SOLVED profile

> ``emit_decimate.decimate_emit_nodes`` (gate O4_EMIT_DECIMATE, default ON,
> last pipeline pass): removes ring vertices 3D-collinear with their kept
> neighbours (XY ≤ 2 cm of the chord AND Z on the interpolated line —
> airside ±2 cm, boundary ±10 cm, justified by the DEM floor: 3-arc-sec
> SRTM ~90 m posts smoothed ~700 m at airports).  Straight runs emit as
> single segments (rect-era economy); vertical transitions/curves keep
> their nodes automatically (off the 3D line).  CONFORMANCE BY
> CONSTRUCTION: a vertex vanishes only if EVERY ring containing it agrees
> (global vote across all shapes, exteriors + holes); tile-seam vertices
> (exact integer lat/lon, minted by tile_cut) force-kept.
> SPJC: **19,665 → 12,441 emitted vertices (−37 %)**, plane/cross/steps
> unchanged.  Law count 13 → 52: NOT new ground — decimation merges short
> segments whose +0.03 noise headroom (proportionally huge at 4-12 m) was
> masking genuinely ~1.6-1.9 % junction runs + the pre-existing 4.0-4.35 %
> tunnel_ramp class; solver-side enforcement of those = queue (same
> post-solve-insert family as junction #166).  NOTE: the 40 T-junctions +
> 1 crossing conformance WARN predates decimation (appeared with the law
> tightening — separate open item).  Suite re-baseline pending.

---

# STATUS — LAW REVIEW (user: "reports 0 but I see violations") — FOUR
# leniencies found + fixed; SPJC honest count = 13 hairline

> USER was right: 7,040 SPJC pairs steeper than 1.5 % were LEGAL under the
> old law (worst: 12.5 % over 5.2 m ruled legal at a nominal 1.5 % cap).
> The four leniencies, all fixed in shared law code (both readers + solver
> inherit):
> 1. **Δs∥ = along-route ARC** (grade_graph.ds_decompose): near curves two
>    physically-close points project far apart along the route → budgets far
>    beyond any surface cap (the perpendicular-to-spine cliffs).  NOW: Δs∥ =
>    foot-point CHORD, so Δs∥²+Δs⊥² = sep² exactly — anisotropy is a
>    rotation, never an inflation.
> 2. **L1 allowance** (`cL·Δs∥ + cT·Δs⊥`) over-allowed diagonals ×√2
>    (4 % road pairs legal at 5.6 %).  NOW: L2 ellipse
>    √((cL·Δs∥)² + (cT·Δs⊥)²) in Allowance.at AND _bake_edge.
> 3. **ELEV_ROUNDING_NOISE_M 0.15 was stale** (sized for 1-decimal emit;
>    on a 5 m edge it allowed cap + 3 %).  NOW 0.03 (2-decimal emit + GS
>    tolerance).
> 4. **Building-frontage pairs inside service_junction faces** took the
>    host's 4 % BODY cap (blend/road-relax exclusions never fired) — the
>    >1 % terminal-side ramps.  NOW: any pair touching a building pad is
>    clamped to config.BUILDING_FRONTAGE_MAX_GRADE (= APRON_MAX_GRADE 1 %)
>    in classify_pair, regardless of host role.
> ALSO: the endpoint-on-spine skip (same-day) was WRONG (unbounded
> side-to-spine differentials) — replaced by INTERIOR-CLEARANCE crossing:
> an intersection within 0.5 m of a chord endpoint is contact, not a
> crossing (distance-thresholded ⇒ reader-stable; also split-agnostic since
> ANY hit point counts, incl. at a sidecar split node).
> MEASURED after all four: SPJC 13 within-shape (all <0.5 % excess), plane
> 0, cross 0; remaining "legal steep" pairs are sub-metre chords where the
> 0.03 noise dominates (cm steps); legal frontage >1 % is 6 (short pairs).
> SUITE UNDER THE TIGHTENED LAW: 20F/325P.  vs the 15F baseline: +1 stale
> unit test (asserted the old arc credit — REWRITTEN as
> test_ds_decompose_never_inflates, green) and +4 expected count-rise
> acceptance regressions = the next drive-to-zero queue: CYXY spine-zero
> ×2 (back red), test_pavement_grade[SPLP], and route_band_zero[SPJC]
> (10 sub-0.4 m ceiling exceedances, ONE junction cluster @(1800,-948)).
> CYXY/SPLP/HECA law-true re-measures also pending.

---

# STATUS — RUNWAY-CONTACT VEER: root cause found + retired under the slice

> USER REPORT (post-round-4 test): spine runway connections veer at the very
> end to a runway SEGMENT corner instead of the edge-contact node.
> MEASURED (scratchpad veer_probe.py, centerline×runway-edge crossings on
> the emitted patch): with the pass on, only **2/18** crossings kept an
> emitted node at the contact and the airside faces touched the runway
> ONLY at segment corners (nearest on-edge vertex = a corner at ALL 18,
> 8–37 m off) — the seam has nowhere to land but a corner.
> CULPRIT (clean A/B attribution): **`_enforce_runway_1to1_sharing`** —
> the rect-era Rule-1 pass replaces every ring-vertex run within 20 m of
> the runway with nearest-segment-CORNER sequences.
> `widen_junctions_to_runway_corners` measured INNOCENT (add-only).
> FIX (v2 — blanket retirement measured WORSE: SPLP's junction↔runway
> seam NEEDS the pass, 4 new >0.5 m steps without it): the pass now
> SPARES SPINE NODES — a ring vertex within 0.5 m of a non-service taxi
> centerline never joins a snap run (same spare mechanism as rect
> corners).  Verified: SPJC contacts keep their nodes (14/25), SPLP back
> to 0 grade + 0 steps.  `widen_junctions_to_runway_corners` measured
> INNOCENT (add-only; debug gates O4_RWY_1TO1 / O4_WIDEN_RWY_CORNERS
> kept).  Remaining non-veer gaps: 3 crossings at 1–1.8 m (grid
> placement, minor) + 3 with NO airside face at the crossing at all
> (source/coverage class — routes crossing the edge over unpaved ground).
>
> OPEN (1 pair, characterized): SPJC law-true 1 — junction #166 chord
> (657,-671)↔(650,-680), 3.94 % over 11.4 m.  b sits 0.028 m ON route-arc
> axis #503 (the slice cut chain -3792..-3798, smooth 27.07-27.14) but is
> NOT in the solver graph; a (=idx 3637, 27.6) never got the tight
> spine-credit pair enforced.  A junction-mesh/spine-credit lockstep edge
> case at a runway-contact arc — NOT the veer mechanism.  proj_lab
> residual1 + veer_probe.py in /tmp/spjc_lab reproduce it offline.

---

# STATUS — ROUND 4 COMPLETE: SPJC **0** law-true (from 1165 at round-1 start)

> **THE FIX THAT KILLED THE 153**: `feasibility_project`'s edge dedup was
> FIRST-EDGE-WINS while the movable-pad flat-group collapse aliases MANY
> physical chords (every pad-ring vertex ↔ one apron node, budgets 10–25×
> apart) onto ONE representative pair — the GS enforced an arbitrary (usually
> loose) duplicate budget while the validator checks each chord at its own
> allowance.  Min-budget-wins dedup (one_solve.py) = correct constraint
> semantics → SPJC 153 → 0 (offline proj_lab proof first, production
> confirmed).  With consistent budgets the GS **converges** (worst 0.025 @
> 800 sweeps → 0.0000 @ 1702; the "oscillation plateau" was this bug) —
> final-pass cap now 2400.
>
> ALSO LANDED THIS SESSION:
> * **3-decimal emit REFUTED** (153→161 — 2-dec rounding was HIDING 8 pairs);
>   hairline tail was never rounding.  Steps 3a/3b + 30-pair forensics all
>   obsoleted by the dedup fix.
> * **Endpoint-on-spine skip** (grade_graph `_ENDPOINT_ON_SPINE_TOL_M` 0.05):
>   a chord endpoint ON a centerline (spine cut/junction node) grades via the
>   spine — same physics as crossing, but a DISTANCE test is mm-stable where
>   `crosses` parity flipped between reader frames (killed the last 122 m pad
>   chord; unmasked the 91-pair aniso class below).
> * **EXACT-AXES SIDECAR** (`axes_exact`/`routes_exact`): to_osm now exports
>   build_context's Centerline objects verbatim (UNSPLIT pts + per-SEGMENT
>   caps + route ordinal); check_grade reconstructs them 1:1.  The legacy
>   per-size-split axes broke shared-centerline membership for long chords →
>   validator refused aniso budgets the solver baked (91 pairs at 1.7 % vs
>   flat 1.5 %).  Readers can no longer drift on splitting/caps/binding.
> * **ITEM B SOLVED**: CYXY apron #120 off-source = `_enforce_runway_1to1_
>   sharing`'s off-source carve FALLING BACK to the uncarved ring whenever
>   the carve split the junction (O4_1TO1_DEBUG prints per-junction carve
>   verdicts).  Fix = split-keep (largest part stays, ≥25 m² real-pavement
>   extras become own junctions).  Also: widen_junctions pav_union fallback
>   (`_source_pav_union` only exists under junction_emit — slice had NO
>   guard) + never-pave-added-ground veto; `_clean_merge` >5 m² notch-chord
>   guard.  Probe point now lands in clearance; 0 off-source shapes.
> * **ADAPTIVE SPINE STEP DEFAULT ON** (`O4_SPINE_STEP_STRAIGHT_M=24`):
>   SPJC ~77-80 s, verts −8.5 %.
> * Coverage probes extended (pipeline post-finalize passes + sloped-rect
>   roles in geom_guard `_ROLES`).
>
> SCOREBOARD (all at new defaults, steps/plane/cross/off-source 0 unless
> noted): SPJC **0**; CYXY **17** (one service_road↔groundside cluster on
> the ~700 m hillside — next round's class); SPLP **0** grade (1 known
> pre-existing source-level off-source apron); HECA **874** (from ~4–5k,
> undissected — playbook next).  SUITE **15F/330P**: two pre-existing CYXY
> failures now PASS (test_cyxy_spine_zero, test_cyxy_spine_zero_no_bowl),
> ZERO new (list diff vs 17F baseline is exactly those two).
>
> NEXT: CYXY 17 (road/groundside solve coupling), HECA by playbook (rate →
> audit → gapcheck), recut SPJC compare-target, modernize
> test_pavement_grade to consume the exact sidecar (it hand-rolls pre-sidecar
> axes and flags 31 junction pairs the law-true check clears).

---

# STATUS — ROUND-4 STEP 1 DONE (`6e0f0c5`): SPJC **178 → 153** (≥1% = 12)

> **LAB TOOLS for the next session: `/tmp/spjc_lab/`** — full_build.py
> (build+law-true check), proj_lab.py (offline projection lab; needs a fresh
> `O4_DUMP_SOLVE_STATE` snapshot + patch since budgets changed),
> vio_forensics.py, node_probe.py, law_diff.py / law_diff_validator.py
> (instrumented law readers), profile_build.py.  Latest patch:
> /tmp/SPJC_round4i.osm (153).

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
