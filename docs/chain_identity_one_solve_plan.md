# Chain identity + one-solve absorption — plan (Fable, 2026-07-09)

USER DOCTRINE (Noah, 2026-07-09, approved plan): all rules and laws
live in `grade_law`; the solver solves as many elevations as possible
in ONE pass; post-solve geometry or elevation mutation is minimized or
eliminated ("this always leads to problems").  Performance is a
first-class goal — the solver is the core of auto_patch and its
largest cost.  Precision reframe: no centimetre precision is needed;
the output is the GRADING UNDER the pavement, which X-Plane layers on
top; smooth results are required, simplification is fine.

## Why (evidence, CYXY 2026-07-09)

The weld ruling (terrain strips share pavement chains) exposed the
architecture debt: shapes are built independently, some solved, some
analytically valued, then five repair passes reconstruct agreement —

* the final `enforce_conformance(tol=0.01)` inserts **3,527 vertices
  into 160 shapes** per CYXY build (identity that construction knew
  and threw away);
* **136 divergent chain sites** still survive into the emitted OSM
  (healthy pre-weld baseline: 1) — measured by the new
  `tools/chain_divergence_audit.py` (T-vertices binned by
  perpendicular offset + near-parallel constrained pairs + coincident
  node ids, classified by role pair);
* the tile mesh Ruppert-explodes: airport-region triangles
  26,727 → 1,552,854 (58×), µm-scale zero-step slivers at the
  divergence sites.

Root mechanisms found (2026-07-09 diagnosis):

1. **Dense weld rows.**  Band/skirt inner rows carry a 5 m station
   at every mid-edge point of the pavement chain — thousands of
   T-vertices for conformance to insert, minting micro-edges that
   `.11f` quantization sharpens into needles; every needle repair
   (removal OR projection — both measured) then desyncs some chain.
2. **Silent conformance bail.**  `enforce_conformance` discarded ALL
   of a shape's insertions when the rebuilt ring went invalid; the
   invalidity came from inserting one candidate into two edges of the
   same ring (`ownset` never updated).  Result: whole welded rings
   never conformed (the immortal `junction~runway_clearance` 53).
   FIXED 2026-07-09 (first-edge-wins + loud bail).
3. **Post-re-cut mutation.**  Legacy clearance `_finalize` merge can
   slide a welded vertex off the pavement chain after the exact
   re-cut (boundary-frozen merge shipped with this slice).
4. **Projection needle repair REJECTED** (measured 136 → 200
   near-parallel pairs): a projected tip moves off its welded HOST
   edge; removal keeps the surviving chord on the host.  Removal +
   chain-consistent partner removal stays.

## The slices

### Slice A — canonical chains + geometry freeze (this session)
* Weld row = the PAVEMENT CHAIN SUBSEQUENCE: pavement ring vertices
  (every ring vertex is already a station: the k = 0 subdivision
  point) plus the run's two endpoint stations.  Interior mid-edge
  stations leave the inner row (they add nodes without information —
  at d = 0 the band value IS the pavement edge value, which lerps
  identically along the pavement's own edge).  Zone-boundary rows and
  outer daylight rows keep full station density.
* No post-clip mutation of shared-boundary vertices: boundary-frozen
  `_merge_coincident` (exact-identity 1e-6), decimation
  keep-predicates, whole-piece drops only.
* Conformance retained ONLY for run-end points (a handful per run,
  the principled residue) — with the duplicate-insert fix above.
* Gate: `chain_divergence_audit` ≈ baseline (1), wedge audit, then
  the CYXY tile-bake triangle A/B (pre-weld 26,727 baseline) —
  the fly-or-abandon verdict for the adjacent-ground project.

### Slice B — solver absorption (next)

**GAP-FILL + DRAINAGE SPINE (user design, 2026-07-09 in-sim review —
the slice B centerpiece).**  For ground ENCLOSED between pavements
(runway ↔ taxiway ↔ stubs), stop marching per-pavement corridor
slabs.  Emit ONE shape per gap:
* boundary = the bounding pavement chains VERBATIM (shared nodes,
  zero new boundary vertices — welded by construction);
* interior = a single SPINE slice parallel to the bounding
  runway/taxiway (the drainage crest/valley);
* the solver grades ONLY the spine nodes (drainage law: deliver the
  runway's drainage, blend smoothly into the taxiway shoulder —
  ruling: zone-3 vertical faces lawful ONLY at a true outer edge).
This replaces hundreds of stationed nodes per gap with a few dozen
solver variables, kills the band↔band clipping in gaps entirely, and
implements the smooth-blend ruling structurally.  Open-terrain-facing
edges (true outer edges) keep the corridor-law march.

**LEGACY CHAIN DELETION (pulled forward).**  The in-sim review found
the legacy ``surface_clearance`` chain is now the DENSITY problem
(CYXY shape 261: ~200 own stations + every neighbour vertex the
final weld inserts).  Bands + skirts + gap-fill supersede it; its
deletion (the old slice-5 item) is the largest remaining node diet.
* Band/skirt construction moves PRE-SOLVE (footprints from the
  DEM-seeded estimate, conservative reach margin); rings immutable
  after construction.
* `graded_strip` / skirt roles join the unified grade graph; the
  corridor envelope (`grade_law.adjacent_ground_envelope`) and skirt
  floor profiles become per-node BOUNDS, transverse/longitudinal caps
  become edges; lift-only DEM clamp = seed semantics.
* THE key unlock: a shared vertex is ONE solver variable — the
  cross-shape weld constraints, emit consensus arbitration,
  authority-vs-soft adoption, and conformance T-vertex insertion all
  stop existing as code.  Pavement-wins (ruling 4) becomes an
  identity, not an arbitration.
* Zone-transition flex (ruling 2): the lip/W rows are solver nodes —
  the solver flexes the cross-section instead of a closed formula.

### Slice C — emit reduction
* Post-solve allowed set shrinks to: whole-piece drops of
  zero-displacement band pieces (triangle diet, chain-safe),
  chain-aware 3D-collinear decimation (one decision per chain), and
  serialization (.11f applies per node — chains move together).
* Delete: analytic emit valuation, `final_grade_projection` as an
  enforcement pass (validators stay, as pure reporters), emit
  consensus, the conformance repair passes.

## Solver performance levers (design-level, per the ruling)

1. **Node diet** (largest, compounding): sparse weld chains (slice A);
   candidate: adjacent-ground station step 5 → 10 m (own gate;
   corridors are 30–75 m wide, zone rows carry the cross-section);
   coarser off-pavement decimation Z-tolerance (0.10 → ~0.25 m —
   under-pavement grading does not need centimetres).
2. **Flatness-gated construction skip** (from the perf arc,
   estimated ~5×): flat airports skip strip/band construction wholesale
   where DEM already satisfies the corridor.
3. **One-pass structure**: absorbing bands into the single solve
   REMOVES whole passes (per-emitter valuation resamplers, final
   projection, conformance welds) — each is a full O(nodes) or
   O(nodes·log n) sweep today.
4. **Cheap constraint forms**: envelope bounds are per-node interval
   projections (O(1) each POCS sweep); band chain topology is sparse
   (2–3 neighbours) — the graph grows ~2× in nodes but far less in
   edges than pavement's all-pair apron cliques.
5. **Convergence tolerance**: solve to ~0.05 m, not mm — fewer
   sweeps; smoothness comes from the laws, not the epsilon.

## Measured lens economics (CYXY tile bakes, 2026-07-09)

The tile bake is CHEAP with warm caches (~3 min via
`tools/run_tile_build.py 60 -136 1 "<Custom Scenery build dir>"` —
the runner now takes the build dir as arg 4) — iterate at the mesh
level, not only the audit.  Hotspot analysis:
`tools/mesh_hotspot_cells.py` bins baked triangles into 25 m cells.

* Slice A round 1 (T-vertices 136→25, near-parallel 136→36) baked
  WORSE than the naive weld: 3,026,007 airport triangles vs 1.55M
  (baseline 26,727).  Fewer sites ≠ fewer triangles.
* Every hotspot cell mapped 1:1 to an audited lens site: ONE
  near-parallel pair costs 10⁵-10⁶ triangles (worst single 25 m
  cell: 1,569,969 triangles from a ~1 mm × 23.76 m seam lens).
  THE GATE IS ZERO near-parallel pairs, not "few".
* Confirmed mint sources round 2: legacy `_finalize` PER-PIECE
  decimation/drop-sharp-corners disagreeing about seam vertices
  (fixed: two-phase collect → shared-seam key set → seam-protected
  mutation), and band edges passing mid-span within 0.2 m of static
  CORNERS with no vertex to snap (fixed: conform-adopt — split ring
  edges at nearby static vertices, then snap all vertices onto the
  static exterior).
* Mesh layer exonerated: `O4_Vector_Utils.insert_node` keys by exact
  (x, y) — coincident wall twins collapse to one node (first altitude
  wins), they do not explode.  Only genuine mm-cm XY lenses explode.
* Pipeline now prints a post-weld residual divergence report (any
  conformance violation surviving the final weld, with lat/lon).

## Status

* 2026-07-09: **SLICE A VERDICT — THE PROJECT FLIES.**  CYXY tile
  bake with the full weld + adjacent-ground bands: **24,333**
  airport-region triangles vs the pre-weld no-bands baseline 26,727
  (naive weld: 1,552,854; slice A round 1: 3,026,007).  Total tile
  633,530 vs baseline 635,934 — the welded bands are triangle-
  NEGATIVE (constrained flat bands replace free DEM refinement).
  Patch divergence: near-parallel pairs 136 → 1 (a legacy
  surface_clearance pair, baseline itself had 1), T-vertices
  136 → 8 (all ≥1 cm legacy/wall classes).
* The fix chain, in landing order: weld-row diet → conformance
  duplicate-insert fix (the silent-bail bug) → boundary-frozen merge
  → conform-adopt (split at static vertices + snap) → two-phase
  seam-safe legacy finalize → NID-LEVEL FINAL WELD in to_osm (the
  keystone: re-runs the T-vertex weld on final nid rings at final
  coordinates, because canonical interning can move vertices AFTER
  the layout-level weld) → self-lens repair (a snapped band rail
  doubling back over itself sub-µm apart).
* Two measured negative results (documented in code): projection
  needle repair (near-parallel 136→200 — tips move off their welded
  host edge; removal is correct) and "few lenses is fine" (ONE lens
  = 10⁵-10⁶ triangles; the gate is zero).
* OPEN after slice A: 8 post-weld crossings (legacy classes, die
  with slice-5 legacy deletion) · skirt check_grade counters (the
  advanced-profile weld-row corner arbitration, values not mesh) ·
  slice B (pre-solve construction + solver absorption) · slice C
  (emit reduction).
