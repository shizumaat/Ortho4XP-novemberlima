# Curve-native spine v2 — cut `pav_union`, retire straight rects

**Supersedes** `docs/curve_native_spine_plan.md` (which proposed a *buffered*-centerline
pavement — abandoned; see Phase rationale). Recognition (P0 of the old plan) is
**done and validated** and is the input to this plan, not part of it.

---

## HANDOVER (2026-07-01) — read this first

**Where we are.** Phases 0, 1, 2 are **built + verified**; Phase 4's spine core works
(corridors grade within cap on all 4 fixtures); Phases 4-BODY, 5, 6, 7 outstanding.
Everything is behind gate `O4_CURVE_NATIVE_SPINE` (default OFF, needs
`O4_RECOGNIZED_CENTERLINES=1` too). **Gate-OFF is byte-identical to HEAD** (verified
SPJC, MD5 `af4211a9…`). All new code committed on branch `troubleshoot`.

**We are PAUSED mid-debug, not mid-phase.** The user stopped forward plan work to fix a
**geometry/spine input bug FIRST** (the principle: get the slice INPUTS right before
chasing output grade). Do not resume Phases 4-7 until this is resolved.

**The open debug thread — over-spurring / centerline fragmentation.** The dead-end
keyhole spurs each free interior centerline end to the nearest boundary. On SPJC the
slice adds **251 spurs (187 dead-end + 64 bridge)** to a 183-centerline network — too
many. Endpoint analysis: of the interior (spur-candidate) ends, **61 sit 3–8 m from
another centerline end** (just beyond the 3 m join tolerance) → likely **fragmentation**
(recognized pieces that should connect but don't, so each end spurs to a boundary
instead), while ~179 are genuinely isolated (SPJC stand lead-ins). Candidate fixes,
pick after inspecting: (a) stitch near-miss endpoints / raise join tol before slicing;
(b) tighten the dead-end rule so short stubs don't spur; (c) something upstream (a
mis-shaped `pav_union` piece — note SPJC's pav_union came out as **19 disjoint outline
pieces + 35 holes**, more fragmentation than expected).

**Inspect the inputs (what the user was about to do).** Three JOSM overlay layers,
same coord frame as the emitted patch, dumped by:
```
O4_RECOGNIZED_CENTERLINES=1 O4_CURVE_NATIVE_SPINE=1 O4_DUMP_SLICE_INPUT=/Users/noah/SPJC_input \
  <venv>/python3 tools/build_target_osm.py SPJC --stage raw --out /tmp/x.osm
```
→ `SPJC_input_pavement.osm` (pav_union outline+holes), `_spine.osm` (183 centerlines),
`_spur.osm` (251 spurs, `kind=deadend|bridge`). The next step is to look at these and
decide which spurs/lines are wrong, then fix the INPUT (not the output).

**Fast metric loop (no full build needed):**
`O4_RECOGNIZED_CENTERLINES=1 <venv>/python3 tools/global_slice_probe.py SPJC` — reports
faces, spine-coverage %, buried length, unshared T-junctions, least-covered lines.
`tools/spine_coverage.py <ICAO>` — coverage % + dangling dead-ends from a real build.

**Key numbers to beat / watch (SPJC, recognition-on):** coverage 37% (baseline rect
spine) → **72%** (slice+dedup+keyholes); unshared T-junctions **0** (de-dup solved
conformance); remaining ~28% is interior apron/junction segments that polygonize can't
edge — those get interior spine nodes at **Phase 4 emit** (see Phase 1 "hybrid" note).

**venv note.** Use the main-repo venv: `/Users/noah/Ortho4XP-novemberlima/venv/bin/python3`.

---

## The model in one paragraph

`pav_union` is already the true pavement footprint (apt.dat row-110 + DSF, minus
runways, with holes) and **already follows every real curve, fillet, and width
change**. We stop manufacturing pavement (straight rects *and* the abandoned
constant-width buffer both fabricate geometry that mismatches source → the
"off-source sliver" defect family). Instead we **cut the real `pav_union` by the
recognized curved centerlines in one global arrangement**; each resulting face is
a grading cell that is guaranteed to carry a spine edge. Faces are classified as
*corridor* vs *apron/junction* from spine topology, not rect geometry. Dead-end
centerlines are closed with a **keyhole tip-cap** so they slice legally and grade
to their tip. The straight-rect builder and bend-split are then retired.

## Why global-cut beats the old per-junction slice

- **Conformance by construction.** One `union_all(grid_size=0.01)` of
  `pav_union` + all centerlines, then `polygonize`, yields faces that share exact
  edges → **no T-junctions, no weld/repair pass**. This side-steps the
  per-junction invariant wall (`vertices_have_source` / `corners_shared`) that
  `curve_native_spine_plan.md` and `curved_runway_crossing_spine` are both stuck
  on — we never repair invariants locally because the arrangement is coherent.
- **No fabricated footprint.** Cutting the real union preserves source geometry
  exactly → the `rests_on_source` / `outside_pavement` sliver family cannot arise.
- **One representation.** No `line` (bend-split piece) vs `route_line` split; the
  centerline is the cut and the spine, end to end.

## Conventions for every phase

- **New gate:** `O4_CURVE_NATIVE_SPINE` (env), default **OFF**. All new geometry
  lives behind it. Gate-OFF must stay byte-identical to HEAD until Phase 7.
- **Fixtures:** SPJC (recognition-validated, 157 CLs), CYXY (curve/apron stress),
  HECA (dense junctions), SPLP (cross-tile seam). Build via the worktree's
  `tools/build_target_osm.py`; validate with `tools/check_grade.py`,
  `tools/mesh_region_tris.py`, `tools/probe_spine_grade.py`.
- **Determinism:** `PYTHONHASHSEED=0`; every metric is measured twice and must match.
- **Each phase is independently buildable and testable** — a phase's "Done when"
  is a green gated test + a recorded metric delta, suitable to hand to `/goal`.

---

## Phase 0 — Baseline & metric harness

**Goal.** Lock a repeatable, per-fixture metric set so every later phase reports a
delta, and add the gate as a no-op.

**Deliverables (measurable).**
1. `O4_CURVE_NATIVE_SPINE` gate added; **gate-OFF byte-identical** to HEAD on all 4
   fixtures (MD5 of built OSM matches).
2. `tools/spine_coverage.py` (new): for a built airport, report
   **spine-coverage %** = (length of recognized centerline that has ≥1 spine node
   within `SPINE_PERP_TOL_M`) ÷ (total recognized centerline length), plus a count
   of **dangling dead-ends** (centerline endpoints interior to `pav_union`).
3. A one-line baseline row per fixture recorded in this doc: within-grade count,
   route-band count, conformance (T-junc / crossings), mesh-tri count,
   spine-coverage %, dead-end count, field-vs-emitted grade along one through-junction
   centerline.

**Done when.** Gate-OFF MD5 matches HEAD ×4 fixtures; baseline table committed.

**Files.** `config.py` (gate), `tools/spine_coverage.py`, this doc.

### Phase 0 results (2026-07-01)

- **Gate `O4_CURVE_NATIVE_SPINE`** added (`config.py`, default OFF). It is not yet
  consumed anywhere, so gate-OFF is byte-identical *by construction*. Verified on
  SPJC with a same-path stash A/B (default env): both builds MD5
  `af4211a9aa68cfe188bc1cd5fa08d59c` — **identical ✓**. (Provably inert for the
  other fixtures too — the symbol is unreferenced.)
- **`tools/spine_coverage.py`** built and working: reports spine-coverage % (union
  of ±9 m arc-intervals around every shape vertex within 1.0 m of a centerline —
  the solver's `_spine_membership` rule) and the dangling-dead-end count.

**Baseline — recognition ON (`O4_RECOGNIZED_CENTERLINES=1`), current straight-rect
spine.** This is the *input* state the new model improves on; low coverage here is
the curved-interior-piece failure the plan targets.

| Fixture | aircraft CLs | total len | spine-coverage % | dangling dead-ends | within-grade (recog-on) |
|---|---|---|---|---|---|
| SPJC | 120 | 35 285 m | **37.0%** | 70 | — |
| CYXY | 14 | 6 581 m | **45.1%** | 5 | 662 (spine 4 / body 658) |
| HECA | 261 | 70 750 m | **55.4%** | 78 | 32 923 (spine 262 / body 32 661) |
| SPLP | 12 | 7 691 m | **19.3%** | 1 | 275 (spine 2 / body 273) |

The recognition-ON within-grade counts are large (HECA 32 923) precisely because the
current spine can't follow the recognized curves through junctions/aprons — Phase 4
must beat both this and the recognition-OFF default. Low coverage (19–55%) is the
core defect the global slice fixes.

---

## Phase 1 — Global slice (cut `pav_union` → faces), dead-ends included

**Goal.** Behind the gate, replace rect+residue decomposition with one global
arrangement: `polygonize(pav_union.boundary ∪ recognized_centerlines ∪ dead-end
keyhole spurs)`. Dead-end support is folded in HERE (was Phase 3) — filtering
dead-ends out to re-add later is wasted work and makes the coverage metric lie.

**Conformance reframe (2026-07-01, user).** "Drive T-junctions to 0" was the wrong
metric, imported from the old separate-shape pipeline. In the new model every face
is born from ONE `union_all` re-noding, so a *genuine* T — a centerline ending on
another — is fully noded into a **shared vertex** on every incident face. That is
legal and desired, not a crack. The only real defect is an *unshared* vertex mid-
edge of a neighbour, which can only come from **near-coincident/dense input**
(overlapping recognized pieces resampled to slightly different vertices). So the
metric is **unshared mid-edge vertices**, fixed by de-duping near-parallel
centerlines — NOT by avoiding legitimate T topology.

**Dead-end keyhole — the working mechanism.** A bare dead-end is a dangling cut and
`polygonize` keeps only minimal CYCLES, so it (and a dead-end reaching a detached
ring) is dropped — no nodes at the tip (verified: ring gives 0 coverage). A medial
line only becomes a face edge when it SEPARATES two regions, i.e. reaches a
boundary. So the keyhole **spurs the tip to the nearest pavement boundary** (the
corridor side-rail, or a stand's building-pad hole); the spur + centerline then run
boundary-to-boundary and the centerline becomes a separating edge with nodes to its
tip. Capped at `_KEYHOLE_MAX_SPUR_M` (40 m) so it never seams an open apron.
(Synthetic dead-end: 22% → 100% coverage.)

**Deliverables (measurable).**
1. New `src/auto_patch/pavement/global_slice.py` (DONE): `build_global_slice_faces`
   (`union_all(grid_size=0.01)` → `polygonize`) + `slice_coverage`; dead-end
   keyhole spurs built in. `tools/global_slice_probe.py` reports the metrics.
2. Faces emitted as `layout.shapes` (roles in Phase 2; for now `ROLE_APRON`).
3. **Unshared mid-edge vertices → 0** after near-parallel de-dup (the real
   conformance metric; legitimate crossings/T's are shared and fine).
4. **Spine-coverage ≥ 95%** of recognized centerline length INCLUDING dead-ends
   (`tools/spine_coverage.py` / `global_slice_probe.py`).
5. No invalid geometry; face count + mesh-tri recorded vs baseline.

**Progress (2026-07-01) — SPJC standalone (`global_slice_probe.py`):**

| step | faces | coverage | unshared T-junc |
|---|---|---|---|
| slice only | 53 | 55% | 62 |
| + keyhole spurs | 72 | 65% | 64 |
| + de-dup (3.5 m) | 145 | **72%** | **0** |

- **Conformance is solved.** De-dup of coincident lines takes unshared mid-edge
  vertices to **0** — clean by construction, no repair pass. (Raising de-dup to
  6 m *lowered* coverage, so the residual gap is not offset duplicates.)
- **Coverage tops out at ~72% from edges alone — and that is expected, not a
  bug.** The least-covered lines are all SHORT interior segments (7–12 m, both
  ends 35–97 m from any boundary): junction-node-to-junction-node connectors
  running through wide apron/junction-throat pavement. A medial line only becomes
  a face EDGE when it separates two regions; an interior apron/junction segment
  separates nothing, so `polygonize` cannot node it. These ~28% are exactly the
  segments the existing junction model carries as **interior spine nodes +
  triangulation**, not as edges.

**Reframed model (hybrid).** Phase 1 geometry cleanly captures through-corridors as
conformant shared edges (0 defects). Interior apron/junction centerline segments are
preserved as constraints and get **interior spine nodes at emit (Phase 4)** — the
same densify-and-triangulate the junctions use today. So the Phase-1 target is NOT
"≥95% coverage from edges"; it is **0 conformance defects + every through-corridor
carried as an edge + interior segments retained for Phase 4** (coverage reaches ≥95%
only after Phase 4 adds the interior nodes).

**Pipeline wiring (2026-07-01) — DONE.** Gated branch in `pipeline.py`: when
`O4_CURVE_NATIVE_SPINE=1` the rect emit, `junction_emit`, and the fillet/synthetic/
`junction_spine` feeders are all bypassed; `build_global_slice_faces` emits the
faces (`ROLE_APRON` placeholder) straight into `layout.shapes`. Measured on SPJC:
- Gated build **completes**, 162 faces, final emit **conformant: 0 residual
  T-junctions / 0 crossings** (the planarize pass ran but had ~nothing to do —
  2 inserts — confirming the "no cleanup needed" thesis) and **deterministic**
  (2× MD5 `40ac70c3…`).
- **Gate-OFF byte-identical**: my `pipeline.py`+`config.py` edits vs HEAD-reverted
  both build `73a1d2…` (isolated from an unrelated pre-existing `junction_spine.py`
  WIP in this tree). Edits are pure `else`-branches, so inert when gate off.
- within-grade 983 (all body/apron) — expected: every face is `ROLE_APRON` at the
  1% cap with no spine grading yet; this is Phase 2 (roles) + Phase 4 (grade).

**Done when.** ✅ SPJC: 0 unshared vertices, conformant, deterministic, gate-OFF
byte-identical. Remaining for full sign-off: run the other 3 fixtures + gate off
the now-redundant planarize/weld passes to prove zero-cleanup (rolls into Phase 6).

**Files.** `pavement/global_slice.py`, `pipeline.py` (gated branch replacing
rect/junction emit), `junction_spine.py` (bypassed under gate).

**Risk.** Holes (building cutouts) must survive the arrangement — verify
`pav_union` interior rings are preserved as face holes, not filled.

---

## Phase 2 — Face classification from spine topology

**Goal.** Recover the corridor-vs-apron signal that rect roles carried today, from
face+centerline geometry instead.

**Deliverables (measurable).**
1. `classify_face(face, centerlines)` → role in {`CORRIDOR`, `APRON`, `JUNCTION`}:
   - **CORRIDOR**: a single centerline runs the face's length and the face is
     within ~half-width of it (narrow, one axis).
   - **JUNCTION**: ≥2 centerlines cross / meet inside the face.
   - **APRON**: a centerline crosses but the face is wide/open (area ≫ corridor).
2. Each CORRIDOR face carries its `axis` (the centerline arc) and `route_idx` so
   the solver's anisotropic along-axis credit still applies.
3. **Classification agreement**: on a *straight-route* airport (no curves), the
   new roles match the current rect/junction roles on **≥90%** of pavement area
   (spot-diff against today's `layout.shapes` roles).

**Done when.** Agreement ≥90% on a straight fixture; every CORRIDOR face has a
non-null `axis` + `route_idx`; roles are deterministic.

**Progress (2026-07-01) — DONE (classifier + wiring).** `classify_faces` in
`global_slice.py`: ≥2 centerlines → junction; 1 narrow (mean width = area/shared-
edge ≤ 50 m) → corridor (carries `axis`); else apron. Wired into the pipeline gated
branch (corridor/junction → `ROLE_JUNCTION`, apron → `ROLE_APRON`, `source_axis`
set on corridors). SPJC gated: 162 faces = **89 corridor / 60 junction / 13 apron**,
build conformant (0 residual T-junc). The ≥90%-agreement spot-check vs the rect
pipeline is still to run.

**Confirms the user's thesis (2026-07-01):** slicing only the recognized centerlines
into `pav_union` yields **no slivers, no residue, and no conformance/weld/planarize
cleanup** — the gated build's final planarize inserts ~1–2 vertices and leaves 0
residual T-junctions, i.e. the faces are already clean. The whole rect-residue
cleanup pipeline is unnecessary under the gate.

**Files.** `pavement/global_slice.py`, `pipeline.py` (gated branch reads the
role/axis). `grade_graph.py` (Phase 4) consumes `source_axis` as the spine.

---

## Phase 3 — (folded into Phase 1)

Dead-end support is now part of the Phase-1 global slice (keyhole spur-to-nearest-
boundary), per user 2026-07-01 — filtering dead-ends out to re-add them later was
wasted work and made the coverage metric misleading. The mechanism and its
validation are documented under Phase 1. Phase numbers below are unchanged for
continuity.

---

## Phase 4 — Solver integration & grade

**Goal.** Grade the sliced faces so corridors hold ≤1.5% (1% preferred) end-to-end
through junctions and aprons, along the true curve.

**Deliverables (measurable).**
1. `grade_graph.build_unified_graph` consumes the new faces: CORRIDOR faces get
   spine edges + along-axis anisotropic credit via `route_idx`; APRON/JUNCTION get
   the visibility-geodesic body model (as today).
2. **Field-vs-emitted grade along a through-junction centerline ≤1.5%** on the
   canonical stress cases (HECA F→05R, CYXY curved taxi E, SPJC recognized spine) —
   the decisive metric from `junction_centerline_spine.md`.
3. **within-grade and route-band counts ≤ straight-route baseline** on all 4
   fixtures (this is where the old plan regressed to ~545; target: beat 486).
4. No new step/cross violations vs baseline; runway-join invariants intact.

**Done when.** All 3 stress cases ≤1.5% end-to-end; within/route-band ≤ baseline
×4; deterministic.

**Progress (2026-07-01) — spine core works, interior nodes + polish remain.**
- **Corridors already grade within cap:** the gated SPJC build reports
  **SPINE(taxi-route) = 0** within-grade violations — because a corridor carries
  its centerline as a face EDGE, the existing solver grades it with no extra work.
- **Coverage 72% → 75.7%** via a two-pass buried-respur in `global_slice`
  (generalises the keyhole: any centerline whose midpoint is buried gets its
  interior endpoints spurred to the boundary, capped at 40 m). Conformance stays 0.
- **Remaining (the hard ~24%):** deep-interior "bridge" centerlines — long
  connectors (e.g. SPJC 550 m) whose endpoints sit 35–98 m from any boundary,
  running through wide pavement and separating nothing. `polygonize` structurally
  can't edge them, the 40 m spur can't reach, and raising the cap seams open
  aprons. These need **constrained triangulation** of their containing face with
  the centerline as an interior constraint (the `triangle` lib is NOT installed;
  note the *current* rect/junction model has the SAME limitation — it also only
  nodes centerlines it can slice into edges). This is the main open Phase-4 piece.
- BODY within-grade is up (junction faces graded all-pair) — needs the corridor/
  apron anisotropic body model wired to the new roles; many are the known-harmless
  short-`d` pinch pairs. Not yet compared to the rect baseline.

**Cross-fixture validation (2026-07-01) — gated builds complete on all 4.**

| fixture | faces (corr/junc/apr) | final planarize residual | within-grade SPINE / BODY |
|---|---|---|---|
| SPJC | 176 (95/68/13) | 0 T-junc / 0 xing | 0 / 1497 |
| CYXY | 32 (8/8/16) | ~0 (2 at footprint stage) | 15 / 935 |
| SPLP | 33 (14/8/11) | 0 / 0 | 0–2 / 328–2631 (2 tiles) |
| HECA | 455 (229/205/21) | 0 / 0 | 24 / 33 132 |

- **Geometry generalises**: every fixture builds, conformant (final planarize
  residual 0), deterministic. The slice model is not SPJC-specific.
- **Spine grades well**: SPINE(taxi-route) violations are small (0–24) — corridors
  hold the cap because the centerline is a face edge.
- **BODY within-grade is the open Phase-4 item**: high everywhere (HECA 33 132 ≈ the
  recognition-on rect baseline 32 661), because junction/apron faces grade all-pair
  at the role cap over terrain relief. Needs the anisotropic corridor + visibility-
  geodesic apron body model wired onto the new roles to actually beat the baseline
  (right now it's ≈ parity, not better).

**DECISION (2026-07-01): interpolation, no new dependency.** For the deep-interior
bridge segments we do NOT add `triangle`/constrained triangulation; they grade via
the surrounding face's interpolation, exactly as the current rect/junction model
already does (same limitation). This keeps "new ≤ old" without a dependency or emit-
model change. The `triangle` route stays on the table if 100% edge-coverage is ever
required.

**Files.** `grade_graph.py`, `route_profile/*`, `grade_law.py`, `pavement/global_slice.py`.

---

## Phase 5 — Coverage fallback (no painted CL, no apt.dat route)

**Goal.** Taxiways with neither a recognized painted centerline nor an apt.dat
1201/1202 route must still get a spine — otherwise that pavement silently degrades
to loose-apron grading (worse than today's rect).

**Deliverables (measurable).**
1. Where a face is CORRIDOR-shaped (narrow, elongated) but no centerline rides it,
   synthesize a spine from the discovered/`TX` edge-skeleton centerline (reuse the
   existing discovered-taxiway extractor) and cut with it.
2. **Fraction of taxiway-width pavement with no spine → 0** on a fixture chosen for
   discovered-only lanes (CYXY has `TX` lanes).
3. No regression to within/route-band vs Phase 4.

**Done when.** No-spine taxiway pavement = 0 on the discovered-lane fixture; grade
counts ≤ Phase 4.

**Files.** `pavement/discovered_taxiways.py`, `pavement/global_slice.py`.

---

## Phase 6 — Retire straight rects & bend-split

**Goal.** Delete the manufactured-pavement path now that faces cover everything.

**Deliverables (measurable).**
1. Under the gate, `_build_taxi_rects`, `split_merged_centerline`, rect
   corner-snapping, merge-collinear, stub rects, and the residue-diff junction emit
   are **no longer called**. The `line` vs `route_line` split collapses to one line.
2. **LOC deleted recorded** (target: the bend-split + rect-snap + merge machinery,
   ~several hundred lines); the diag tools that read `(line, ref)` tuples updated
   or removed.
3. Full suite green under the gate; the retired functions are dead only under the
   gate until Phase 7 (keep gate-OFF path intact for byte-identity).

**Done when.** Gated build uses zero rect/bend-split code; suite green gate-ON;
gate-OFF still byte-identical to HEAD.

**Files.** `pavement/rects.py`, `pavement/centerlines.py`, `pavement/stubs.py`,
`junction_emit.py`, `junction_spine.py`, affected `tools/*`.

---

## Phase 7 — Re-baseline & flip default ON

**Goal.** Prove the new model beats the straight-route baseline everywhere, then
make it the default (byte-identity guarantee ends here — this is architecture, not
cleanup: re-baseline the suite per project rule).

**Deliverables (measurable).**
1. Per-fixture comparison table (all 4): within-grade, route-band, conformance,
   mesh-tri count, spine-coverage, field-vs-emitted grade — **new ≤ old on every
   grade metric, conformance ≤ old, coverage ≥ old**.
2. Determinism proven (2× MD5) on all fixtures.
3. Mesh-tri count within budget (`tools/mesh_region_tris.py`) — the global slice
   must not explode triangle count vs the rect model.
4. Recut fixture `*_target.osm` where geometry legitimately shifted; gated-on
   acceptance test added; **default flipped to ON**; old gate-OFF path + retired
   code deleted.

**Done when.** New beats old on grade+conformance+coverage ×4, deterministic, mesh
budget met, default ON, dead code removed, suite green.

**Files.** `config.py` (default ON), `tests/test_compare_target.py`, fixture recuts,
this doc (final numbers table).

---

## Metric cheat-sheet (what "measurable" means here)

| Metric | Tool | Target |
|---|---|---|
| Conformance (T-junc / crossings) | `check_grade.py` | 0 / 0 by construction (P1+) |
| within-grade / route-band | `check_grade.py` | ≤ straight-route baseline (P4+) |
| Field-vs-emitted along centerline | `probe_spine_grade.py` | ≤1.5% through junctions (P4) |
| Spine-coverage % | `spine_coverage.py` (new) | ≥95% (P1) → ≥99% (P3) → 100% taxi (P5) |
| Dangling dead-ends | `spine_coverage.py` (new) | 0 route-ridden (P3) |
| Mesh-tri count | `mesh_region_tris.py` | within budget vs rect model (P7) |
| Invalid geometry | `explain_validity` sweep | 0 (P1+) |
| Determinism | 2× build MD5 | identical (every phase) |
| Gate-OFF byte-identity | build MD5 vs HEAD | identical (P0–P6) |
