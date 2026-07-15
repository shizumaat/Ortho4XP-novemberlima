# Slice B — solver absorption: staged decomposition (Fable, 2026-07-10)

Companion to `docs/chain_identity_one_solve_plan.md` §Slice B (the intent) and
the STATUS.md part-35 handover (the acceptance criteria).  This document is the
DESIGN PASS Noah's part-35 ruling ordered before orchestration: it decomposes
the absorption into five stages, each independently gated, each small enough to
be one work order with a fresh-trace mandate and numeric baselines.

USER DOCTRINE (binding, from part 34): all rules and laws live in `grade_law`;
the solver solves as many elevations as possible in ONE pass; post-solve
geometry or elevation mutation is minimized or eliminated.  A shared vertex is
ONE solver variable.  Pavement value always wins at pavement nodes — as an
IDENTITY, not an arbitration.

## Consolidated acceptance criteria (from the part-35 handover)

1. Legacy-off gate table clears: `O4_LEGACY_SURFACE_CLEARANCE=0` with tears 0
   (was 13), patch nodes BELOW 4,420 (legacy-off today INVERTS to 6,351),
   coincident single digits (was 344, of which 341 band↔band clip-seam twins),
   no new sub-100 millimetre T-vertex classes, no new near-parallel pairs.
2. `O4_CLEARANCE_CHARTER` ON with ZERO new adjacent-ground tears (today it
   trades blobs for tears, 0→10 at CYXY); taxiway_clearance area −60%.
3. Hangar blob #210 and notch blob #236 heal.
4. Taxiway-end wrap: adjacent-ground coverage runs the whole taxiway, wraps
   the taxiway end maintaining clearance distance, joins the runway-end skirt
   smoothly (Noah ruling 2, site 60.6972471,-135.0608669).
5. `final_grade_projection` retires as an enforcement pass (it caused BOTH
   round-6 solver-side defects; the pad-host relevel and spine-edge clamp
   gates are patches over its damage and retire with it).
6. Strips stand off tunnel ramps like buildings (the 2 ledgered SPJC tears).
7. SPJC `no_self_overlap` and within-shape `pavement_grade` reds burn down.
AMENDED (Noah ruling 2026-07-14): the B4 FLIP GATE is the
   explosion-relevant subset — tears 0, zero new near-parallel and
   T-vertices, clean forced-re-bake triangle check, in-sim pass.  The
   HYGIENE rows (coincident twins, node diet, adoption/weld zero-hit
   counters) are MEASURED AND LEDGERED to the slice-C emit
   restructure (bands emitted from the solver grid), not flip
   blockers: exact-coordinate twins collapse to one node at bake
   (the part-34 mesh exoneration) and holding the ~36 s builds
   behind the restructure serves nothing.
8. PERFORMANCE (Noah ruling 2026-07-10): the absorption must SIMPLIFY and
   REDUCE steps, never add complexity — and the full CYXY build returns
   UNDER 2 MINUTES (120 s).  Measured baseline at dev 162aaca: 195.7 s
   wall, per-phase (build_time_model store, newest record, total 203.1 s):
   "Emitting terrain features & finalizing" 127.7 s (62.9%) — the
   post-solve march this design deletes; "Solving elevations" 70.5 s
   (34.7%); all phase-1 geometry ~5 s.  The target is therefore
   structural, not aspirational: retiring the emit march funds it even
   with solver node growth.  PER-STAGE GATE: no stage may regress full
   CYXY build wall-time beyond noise (record the build_time_model phase
   split in every stage report); if the solver becomes the new bottleneck
   at B3, apply the levers section (interval bounds are O(1) projections;
   convergence tolerance 0.05 m; zone-row station step 10 m).

## Architecture facts the design builds on (verified 2026-07-10 in this tree)

* `elevation_per_surface/route_profile/solve.py::solve_route_profile` is THE
  only pass that sets pavement elevations.  Free nodes carry
  `[floor, ceiling]` bands (`building_feasibility.reach_band_unified`) and
  edge budgets (`cap · length`) enforced by a projected Gauss-Seidel sweep
  (`one_solve.py`; a vectorised Jacobi variant exists behind
  `O4_FP_VECTORIZE`).  Hard anchors: tile seams, runway profile nodes,
  building seats, and — the precedent that matters — object-bridge plates.
* THE ABSORPTION PRECEDENT: `ROLE_BRIDGE_TRENCH` / `ROLE_BRIDGE_CAUSEWAY` are
  already first-class graph members (`solver_primitives.PAVEMENT_ROLES`):
  ring vertices enter the canonical node registry and are HARD PINS at values
  written at shape birth (`layout._object_bridge_pin_values`).  The solver
  grades neighbouring pavement to meet them and never reshapes them.  Every
  stage below reuses this admission pattern, varying only whether the new
  nodes are pins (stage B1) or free variables with law bounds (B2, B3).
* Vertex identity = `canonical_points.CanonicalPointRegistry` (0.5 metre
  interning), seeded from apt.dat pavement vertices; `to_osm` re-interns and
  runs the nid-level final weld (the slice-A keystone).
* `grade_law.adjacent_ground_envelope(role, code_number, code_letter, d)`
  returns `(floor_offset, ceiling_offset)` RELATIVE TO THE PAVEMENT-EDGE
  ELEVATION at lateral distance `d`.  The edge elevation is itself a solver
  variable — so the envelope is NOT a static per-node band; it is a COUPLED
  constraint between a terrain node and its host pavement edge station.
* The post-solve march in `pipeline.py` (approximate anchors in today's
  tree): legacy clearance cuts ~5520 → conformance ~5619/5735 →
  `final_grade_projection` ~5862 → `emit_runway_end_skirts` ~5950 →
  `emit_gap_fill_spines` ~6010 → `emit_adjacent_ground_bands` ~6040 → final
  conformance + emit consensus ~6096 → `to_osm`.  Each emitter VALUES its
  nodes analytically (`_edge_interp_alt`, `_nearest_pav_alt`, envelope
  reads), then the weld/adoption/consensus apparatus reconstructs agreement
  that construction threw away.  That apparatus is what absorption deletes.

## The one new solver primitive: interval edges

Today's edge constraint is a symmetric slab `|z_i − z_j| ≤ budget`.  The
envelope law needs a SIGNED interval: `lo ≤ z_i − z_j ≤ hi` with `lo`/`hi`
independently optional (a `None` ceiling permits any rise, a `None` floor any
drop — the law's own semantics).  Projection onto a signed slab is the same
O(1) operation the Gauss-Seidel sweep already does (project the difference
into `[lo, hi]`, split the correction by the endpoint hardness weights); slabs
are convex, so POCS convergence is unaffected.  The symmetric case is
`lo = −budget, hi = +budget` — the existing form embeds exactly, so the
extension can be landed with a byte-identity guarantee for existing inputs.

This primitive also expresses:
* skirt/floor profiles: `z_node ≥ profile_value` = interval edge to a
  constant (or a one-sided per-node bound where the value is birth-computable);
* the lift-only DEM clamp as SEED semantics (seed at DEM, floor at DEM,
  ceiling open) — per the plan;
* transverse/longitudinal band caps as ordinary symmetric edges.

## The stages

Ordering is forced by parent relationships: bands clip against skirts, and
gap faces take building pads AND runway-end skirts as gap parents — so skirt
footprints must exist pre-solve before gaps construct pre-solve, and both
before the band march is replaced.  Skirt values derive from the runway
profile, which is solved BEFORE `solve_route_profile` (runways are hard
anchors) — so skirt construction can move pre-solve without a value cycle.

### Stage B0 — solver primitive + admission scaffolding
* Interval-edge form in `shape_constraints` + both projection paths (scalar
  Gauss-Seidel and the vectorised Jacobi variant), symmetric-embed
  byte-identical (existing edges produce bit-identical solves; A/B gate).
* Role-admission scaffolding: a declared set of TERRAIN GRAPH ROLES admitted
  to the canonical registry and node list, per-role sub-gates under one
  master gate `O4_ONE_SOLVE_TERRAIN` (default OFF until B4).
* Synthetic unit tests: interval projection, one-sided bounds, hard-endpoint
  weights, convergence on a mixed symmetric/interval graph.
* Gate: full suite green-modulo-known-14; chain_divergence_audit A/B
  byte-identical patch (gates off = inert).

### Stage B1 — runway-end skirts absorbed (the pin pattern, smallest risk)
* `emit_runway_end_skirts` construction (footprint + ring geometry) moves
  pre-solve; rings immutable after construction; ring vertices join the
  registry (shared boundary vertices with pavement now ARE pavement
  variables — identity, not weld).
* Skirt values are birth-computable from the already-solved runway profile
  (inverse-RESA law): reuse the bridge-plate HARD PIN path verbatim.
* The skirt-vs-band clip arbitration and skirt conformance welds die for
  this role; gap parents read the same pre-solve shapes.
* Numeric gate at CYXY: check_grade tears/cross-shape/vertex-to-edge/mid-edge
  all 0 (unchanged) · skirt edge-grade counters ≤ part-36 item-4 outcome ·
  audit zero new near-parallel/T-vertices · patch nodes ≤ 4,420 reference ·
  forced re-bake (O4_AUTO_PATCH_REBUILD=1) airport triangles ≈ 15,037
  reference, hotspot cells only the 3 known legacy sites.

### Stage B2 — gap-fill spines absorbed (first FREE terrain variables)
* Gap face construction moves pre-solve (pavement-union interior rings +
  B1 skirt parents + pad parents; boundary verbatim as today).
* Spine nodes become free solver variables: envelope INTERVAL edges to their
  bounding pavement chain stations (per bounding parent, per the module's
  existing per-parent envelope reads), longitudinal smoothness edges along
  the spine (`TAXIWAY_MAX_GRADE_CHANGE_PER_M`-family caps), DEM seed.
* CORRECTED BY FRESH TRACE (2026-07-10 evening, B2 scout): the
  "sanctioned spine-endpoint T-vertex insertion" was a STALE docstring —
  the live design is the part-34 OPEN-WAY float (the spine floats ≥2 m
  off the ring; landing geometry was retired because it minted the
  96-millimetre shallow-landing sliver class).  Spine ends KEEP floating;
  conformance insertion count is 0 both ways by construction.  Spine
  nodes are INTERIOR points, not ring vertices, so absorption needs a
  dedicated spine-node admission path beside the B0 ring hook (a
  pre-solve spine store on the layout, admitted under the gap sub-gate).
  Longitudinal law: the analytic quarter-up-from-floor + clamped
  Laplacian dies; the solver applies the project's existing spine
  curvature law (TAXIWAY_MAX_GRADE_CHANGE_PER_M second-difference
  fairing) under the envelope interval edges — spine VALUES move off the
  analytic target (lawful within the corridor; in-sim review judges).
* Deletes: gap analytic valuation; the gap share of the final weld.
* Gate: B1 gate plus gap faces 17 reference (census unchanged), residual
  divergence report has no gap_fill entries, and the round-4/round-6 gap
  sites verified in the bake.

### Stage B3 — adjacent-ground bands absorbed (the big one)
* Band FOOTPRINTS construct pre-solve from the DEM-seeded estimate with a
  conservative reach margin (the plan's directive); rings immutable; inner
  row = the pavement chain subsequence (slice-A law) — now shared VARIABLES.
* Zone rows become free variables — CORRECTED BY FRESH TRACE (2026-07-11,
  order-2 scout; the original "transverse/longitudinal caps become edges"
  came from the plan doc and is WRONG for bands): the band value law is a
  pure PER-VERTEX two-sided envelope clamp of the DEM
  (`_make_edge_projection_resampler`; `ROLE_GRADE_LIMITS["graded_strip"]`
  is None — no within-shape rule, no neighbour coupling, no fairing).  The
  encoding is therefore ONE two-sided envelope interval edge per zone node
  to its host pavement edge station plus the DEM seed, and NOTHING else —
  projection of the DEM seed onto the slab reproduces the analytic clamp
  exactly (parity by construction).  Daylight benching
  (`adjacent_ground_supported_depths`) stays construction-side (it shapes
  footprint depths, not values).  The seam-taper pin is ALSO footprint
  machinery (terminal-station daylight depths) — identity does NOT retire
  it; it keeps firing unchanged.  Identity retires 2 of 3: the value
  adoption gate and the band-corner weld go to zero hits gate-ON; the
  seam-taper pin stays.
* Taxiway-end WRAP (acceptance 4): the band corridor continues around the
  taxiway end at clearance distance and lands on skirt ring vertices —
  construction geometry in this stage, values free variables like any band.
* Tunnel-ramp standoff (acceptance 6): band construction excludes a 1 metre
  standoff around bridge/tunnel ramp shapes, the building-standoff pattern.
* Deletes for graded_strip: analytic valuation, value adoption gate, emit
  consensus arbitration, conformance T-vertex insertion, the band↔band
  clip-seam classes (the 341-twin class becomes unrepresentable: one
  variable cannot disagree with itself).
* Expected solver growth: nodes roughly ×2, edges much less (band chains are
  2–3-neighbour sparse); apply the performance levers if needed (zone-row
  station step 5→10 metres has its own gate; convergence tolerance 0.05 m).
* Gate: full legacy-ON regression first (bands absorbed, legacy still
  present): audit floor unchanged, bake ≈ reference, check_grade zero-family
  unchanged.  THEN the legacy-off gate table measured (expect most of
  acceptance 1 to clear here).

### Stage B4 — charter ON + legacy clearance deletion
* Flip `O4_CLEARANCE_CHARTER` (wingtip clearance along taxiways/runways
  only); the absorbed bands now hold the steep terminal terrain the charter
  removes from clearance (the part-35 blocker in miniature — this is the
  test that absorption actually grades what clearance was holding).
* Retire the legacy `surface_clearance` chain for everything the charter
  excludes (the old slice-5 deletion, the largest node diet), then
  `O4_LEGACY_SURFACE_CLEARANCE=0` as default.
* Gate: the FULL acceptance list — gate table (tears 0, nodes < 4,420,
  coincident single digits), charter criteria (area −60%, zero new tears),
  blobs #210/#236 healed in the bake, wrap form verified at the ruling-2
  site, the 3 legacy near-parallel audit-floor sites GONE (they are legacy
  classes), suite reds burn-down measured (acceptance 7).

### Stage B5 — projection retirement + apparatus measurement
* Measure `final_grade_projection` as a no-op on the absorbed tree (its
  writes should be empty or sub-tolerance); retire it as an enforcement pass
  (validators stay, pure reporters — the part-30 architecture ruling), and
  retire `O4_PAD_HOST_PAVEMENT_LEVEL` / `O4_SVC_SPINE_EDGE_COUPLE` with it
  (patches over its damage).
* Inventory the now-dead weld/consensus/adoption/conformance code paths with
  measured zero-hit counters across the test airports — the deletion itself
  is SLICE C scope, but the evidence ships here.
* Gate: byte-level A/B with projection off vs on (PYTHONHASHSEED pinned);
  in-sim review round 7 (Noah) before any fixture recut (standing ruling).

## Criterion-8 attribution CORRECTED (order-2.5 scout, 2026-07-11)

Measured decomposition of the gate-ON CYXY build (189.9 s total):
* "Emitting terrain features & finalizing" 91.5 s = **96%
  `emit_surface_clearance_cuts`** (the legacy surface_clearance chain,
  87.7 s, dominated by `hole_router`).  The adjacent-ground band emit is
  0.39 s; gap spines 0.31 s; `to_osm` 0.35 s.  The criterion-8 "retiring
  the emit march funds the target" claim is TRUE but the march in
  question is the LEGACY CLEARANCE march — deleted at stage B4 — not the
  band march, which is already sub-second.  Post-B4 projection:
  ~150 s − ~87 s ≈ 65-75 s, well under the 120 s target.
* "Solving elevations" 95.2 s is NOT the solver: the POCS projection
  solve is **2.4 s** even with 7,130 terrain variables.  The phase is
  filled by `object_terrain_assembly.attach_bridge_classification`
  (39.7 s, fires at pipeline phase 5; the KBNA feature worktree carries
  a 53× classifier speedup + pack-sidecar cache — a merge-time lever
  outside this slice) and the reach-band feasibility machinery
  (`_nearest_visible_centerline` / `_paved_frac`).
* Order-2's "DEM scans dominate the band emit" was also false: within
  the 0.39 s emit, DEM sampling is ~8%; clip + polygon assembly ~50%.
* The seed-vs-solved coverage gap (24 missed shapes / 1,285 analytic-
  fallback vertices) is re-scoped as a QUALITY ledger item (values move
  to solved, in-sim-gated), not a performance lever.  It can ride with
  order 3 or B4 preparation.

## Post-B1 corrections (Noah rulings, 2026-07-10 evening)

* The B1 "reverse ordering dependency" ledger item is NARROWER than the
  agent reported: boundary ribbons and boundary→DEM bridges are RETIRED in
  effect (`finalize.emit_terrain_transition_features` skips both under the
  adjacent-ground law, default ON since 2026-07-08) — those clip targets are
  empty sets by construction.  The vestigial ribbon code and
  `_reconcile_boundary_bridges_with_skirts` are slice-C deletion candidates.
  Real post-solve neighbours of a pre-solve skirt: groundside pavement,
  tunnels, clearance cuts.
* RULING — skirt airside precedence: the runway-end skirt area is
  inherently AIRSIDE; nothing there can legitimately be groundside.  The
  skirt NEVER clips against groundside (clearance.py historically did
  `region.difference(groundside_block)` — backwards); GROUNDSIDE clips
  around the skirt, exact footprint, shared chain verbatim, no buffer gap.
  Inert at CYXY (zero overlap); a named acceptance criterion before any
  default flip (B4).

## Work-order boundaries

B0 and B1 are each ONE work order (B0 solver-internal, B1 first consumer;
B1 validates B0's design before anything larger builds on it).  B2 is one.
B3 splits into three: construction move (footprints pre-solve, values still
analytic — byte-comparable), variable admission (values from the solve), and
wrap+standoff geometry.  B4 splits into charter flip and legacy deletion.
Every order carries: fresh-trace mandate, the stage gate's numeric baselines,
foreground-only, worktree HEAD verification, no commits (serial integration
in the main checkout with audit A/B per landing — the part-35 protocol).

## Open questions (resolve during B0/B1, none block starting)

1. Envelope host-station mapping: a band node at distance `d` couples to
   WHICH pavement edge station once stations are variables — nearest station
   at construction time (frozen mapping, simplest, matches today's marcher)
   versus the two bracketing stations with interpolation weights.  Start
   frozen-nearest; revisit if benching artifacts appear.
2. Registry tolerance (0.5 m) versus band station spacing (5 m, possibly
   10 m): fine as-is, but B3 must assert no unintended cross-row interning.
3. Whether skirt pins should be true hard pins (B1 proposal) or floor-only
   bounds letting the solver lift skirts to meet pavement — STATUS records
   skirts as NON-FLAT profile AUTHORITIES, so pins; revisit only if B1's
   gate shows skirt-adjacent tears.
4. KDFW/HECA scale check timing: CYXY is the gate airport throughout; run
   the big-airport solver-performance check once at B3 (the node-growth
   stage), not per-stage.
