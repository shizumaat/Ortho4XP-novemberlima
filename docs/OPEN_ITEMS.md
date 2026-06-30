# OPEN_ITEMS — live unfinished work distilled from the docs/ plan audit (2026-06-30)

This is the single index of **planned-but-not-done work that is still relevant**, lifted
out of ~30 plan docs (many of which are now superseded — see `docs/archive/README.md`).
Each item cites the source doc and the code evidence. It is *not* a status of the live
solver (that's `STATUS.md`); it's the backlog of things the plans promised and the code
doesn't yet have.

Ordering is rough priority. Tick items off here as they land.

## 1. Solver correctness / the "zero-violation" loop
These close the project principle *"no airport is legitimately infeasible — every grade
violation is a solver miss"* (`memory: runtime_vs_test_grade_gap`).

- [ ] **Zero-violation hard gate (W6).** The build never *fails* on a within/route grade
  violation; OSM-grade validation is DEBUG-only behind `O4_VERIFY_OSM_GRADE` (default off)
  and explicitly "never raises" (`verification.py:993`). Make `check_grade` a post-solve
  assertion that fails the build. — *grade_enforcement_plan.md (W6)*
- [ ] **Feasibility-restoration cascade (W3) + geometry feasibility prepass (W5).** No
  "steepness sink" cascade (yield anchor → flex corridor → split carrier shape at the grade
  break with a ≤4 % transition element/wall), and no convex-split prepass for non-convex
  aprons/junctions. Zero grep hits for any transition/grade-break/disconnect machinery.
  — *grade_enforcement_plan.md (W3/W5); heca_zero_violations_plan.md Steps 1–5 (archived)*
- [ ] **Route-band check on the shipped OSM patch.** The route-band law is confirmed only
  in-memory on graph `G`; the "purist" path — reconstruct `G` from the shipped per-tile OSM
  patch inside `check_grade` — was never wired. Explicit follow-up comment at
  `tools/check_grade.py:1329`. *(Recurs across 4 docs — the most-cited open item.)*
  — *grade_law_consolidation_handover.md/_2, route_field_model.md, route_profile_solver_status.md*
- [ ] **Audit every `check_grade` rule against the shared law (Item 4).** `_check_plane_gradient`
  (`tools/check_grade.py:440`) and the cross-shape proximity / vertex-to-edge / edge-midpoint
  checks are test-only rules with no `grade_law`/solver counterpart — never reconciled or
  retired. — *grade_law_consolidation_handover.md/_2/_4*

## 2. Known cliff/grade families still open
- [ ] **Building-anchored apron >~60 m from its building drapes to DEM** (15.5 % cliff
  family). Flagged open and never confirmed resolved after the connectivity fix.
  — *grade_law_consolidation_handover_2.md/_4 (TODO 6)*
- [ ] **#198 switchback strip should be decomposed as a road** (4 % grading + junction +
  sloping rects); the edge-retreat symptom-patch still stands. — *network_profile_model.md (archived dir? no — in place); see also memory `network_profile_model_built`*
- [ ] **"No shape may EVER check grade across grass."** The across-grass coupling class was
  flagged for root-cause removal in *both* solver and validator together; partially handled
  by the grade_law reach-band rework but never closed as a guarantee. — *network_profile_model.md (USER RULING)*
- [ ] **SPJC spine 0→2 regression** + pre-existing suite reds (`test_apron_with_spine`
  baseline shift, `test_pavement_grade`). Acknowledged-open, not feature-blocking.
  — *route_profile_solver_status.md, anisotropic_edge_handling_plan.md (P7)*

## 3. Producer/data-model gaps
- [ ] **Convert the OSM taxi-centerline producer to `TaxiCenterline`.** Only the apt.dat
  producer was converted (`apt_dat_reader.painted_taxi_centerlines`); the OSM producer
  `pavement/centerlines._extract_osm_taxi_centerlines` (`centerlines.py:336`) still returns
  `list[tuple[LineString, str]]` — the exact gap the doc warned would break painted-only
  airports. — *grade_law_consolidation_handover_4.md (TODO 3)*

## 4. Never-built features (re-spec before building — specced against dead module layout)
- [ ] **Feature C — tunnel/overpass crossing patches** (`generate_crossing_patches`,
  `auto_patch_crossings`, `tunnel_clearance`/`overpass_clearance`, `layer` tag). Zero grep
  hits. Note tunnel *portals* were built separately (`bridges.py`); the general
  crossing-detection feature was not. — *auto_patch_tier2_plan.md (archived banner)*
- [ ] **Feature D (Tier 3) — DSF custom-mesh import** (`parse_dsf_mesh`,
  `generate_mesh_import_patches`, `custom_mesh_path`). Entirely unbuilt. — *auto_patch_tier2_plan.md*

## 5. Dead-code cleanup (deferred; tracked under cleanup M7b/M8)
The 2026-06-30 audit already removed the fully-orphaned `interior_path.py` (+ test + flag),
`grade_graph.flatten_pairs`, and three dead solver imports (`FIELD_TARGET_CONFORMANCE`,
`BUILDING_ROUTE_FEASIBILITY`, `MIN_GRADE_NETWORK`). Still outstanding:

- [ ] **Remove the gated-off apron back-edge-ramp machinery.** `_apron_back_band_nodes`
  (`solver_primitives.py:315`) returns `{}` under the default config and its validator
  exemption is already deleted — but it's intertwined with the *live* `APRON_BACK_EDGE_GRADE`
  (used at `grade_graph.py:523`), so removal needs care (not byte-trivial). — *apron_back_edge_ramps.md*
- [ ] **M7b — modularize `junction_repair.py`** (still one 3,923-line file; 14 `junction_repair`
  imports in `pipeline.py`; acceptance wanted 1). Blocked on M6. — *cleanup_consolidation_plan.md*
- [ ] **M8 — modularize `pipeline.py` (4,721 lines) / `elevation.py` (3,229 lines).** Lowest
  priority, blocked on M6. — *cleanup_consolidation_plan.md*
- [ ] **M6 — reach an acceptable solver baseline / tag `baseline-clean`.** Open *by design*
  (the live research problem); the aniso-edges + clearance commits are M6-class work. M7b/M8
  gate on it. — *cleanup_consolidation_plan.md*

## 6. Banked feature flags (defined, intentionally not wired)
Kept in `config.py` as vehicles for §1–§3 work; not dead, not live:
`FIELD_TARGET_CONFORMANCE`, `BUILDING_ROUTE_FEASIBILITY`, `MIN_GRADE_NETWORK` (P4/P5
building-as-driver — *taxi_centerline_grading_plan.md*), `O4_FRONTAGE_SPINE_RISE`
(`route_profile/anchors.py`). Wire or delete when the corresponding §1/§2 item is decided.
