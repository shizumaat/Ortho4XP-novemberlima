# grade_law consolidation — handover #4 (continues handover #3)

Read `docs/grade_law_consolidation_handover_3.md` first (and #2, #1). This records
the **2026-06-29 (continued) session**. Goal unchanged: ONE canonical ruleset both
the solver and validators use; no airport legitimately infeasible.

Memory: `taxi_centerline_connectivity_model` (the headline change), plus
`spine_rise_to_building_region` (now superseded — see below).

---

## ★★ RESOLVED — "the spine does NOT rise to serve a building ACROSS an apron"

Handover #3's big OPEN item. **The framing in #3 was wrong** — it is NOT a
"region-lift" problem, and the 5 reverted approaches in #3 (soft floor, hard-pin,
POCS, synthetic edge to nearest/serving node) were all chasing a SYMPTOM. **Do not
re-try them.**

### The real root cause (CYXY ~U12 / F)
`apt_dat_reader.taxi_centerlines` GROUPED taxi edges **by NAME** then linemerged
per-name. CYXY's continuous physical route changes name at node E = local
(-527.4, 997.8) = `60.7185137,-135.0770131`: the unnamed apron lane (`~U12`)
continues end-to-end into named taxiway `F` at a **degree-2** node. Grouping by name
**severed one continuous route into two polylines**, each TERMINATING at E inside an
orphan 640 m² apron. Each was then a DANGLING cut (`junction_spine._partition_junction`
only partitions on a boundary-to-boundary through-cut), so the apron was never sliced,
no spine node was seated at E, `~U12` dead-ended (node580 deg-1), the orphan stayed a
mis-classified apron seated low → that low seat is what broke the spine. Both edges
are `taxiway_D` (same size); ONLY the name differed (user's diagnostic question: "if I
named the unnamed section F, would it change?" → YES). It was a data-categorization
artifact, never a geometry/solver bug — the geometry the user inspected was fine.

### The fix — taxi centerlines built BY CONNECTIVITY, not name
Full refactor (memory `taxi_centerline_connectivity_model`; ~20 files, net −91 lines):
- **`apt_dat_reader.TaxiCenterline`** dataclass: `.line`, `.seg_sizes` (PER-SEGMENT
  ICAO letter from each edge `kind`), `.is_service`, `.name` (LABEL only). Helpers
  `size_at_arc / size_at_point / dominant_size`.
- **`taxi_centerlines` rebuilt**: walk maximal degree-2 chains of sized taxi edges
  (IGNORE name); split ONLY at junctions (network degree ≥ 3) + runway contacts;
  carry per-segment size; bend-split each route via `split_merged_centerline` for the
  rect decomposition (bend splits land on shared VERTICES, so the spine still connects
  across them — only the NAME-split at a vertex-less interior point was the bug).
- All producers emit `TaxiCenterline`: `service_road_centerlines` (is_service=True),
  `discover_unreferenced_centerlines`, `synthetic_junction_spine`. `pipeline` stores
  `layout.apt_taxi_centerlines: list[TaxiCenterline]` (the SPINE/size model) and
  projects a `(line, name)` view for the rect pipeline (rects don't use size).
- Size consumers read it: `grade_graph.Centerline` carries per-segment `seg_caps` +
  `cap_at(arc)` (`_build_global_spine` uses the midpoint cap); `building_feasibility`
  band credits `size_at_arc(sp)` at the foot.
- **Per-segment VALIDATOR caps**: `grade_graph_validate._spine_runway_join_violations`
  uses the size at the contact ENDPOINT; `verification._per_axis_allowance` splits a
  route into per-size sub-axes. (Main within-shape validator = `build_context` →
  `cap_at`, already per-segment.) `_collect_junction_axes`'s cap is discarded by its
  only caller (gated legacy) → no lockstep concern.
- **DELETED dead name machinery**: `apt_taxi_letters` field, `taxi_size_letters`,
  `unnamed_edge_component_names`, `_split_polyline_at_junction_vertices`.
  `taxi_shape_code_letter` now MEASURES the rect short-edge width.

### Result (CYXY, `build_airport_pavement`, production path)
- `~U12` connects directly to `F` (node692 at E, deg-6); the 640 m² orphan apron is
  GONE — fixed at the source, no reclassification hack.
- **spine within-violations = 0**; node B (104.7,-806.6) preserved deg-2 (the merge-
  approach collateral that dropped it off-spine is absent).
- total within-violations **295 → 269**; the catastrophic orphan cliffs (127 %/175 %)
  are gone. The remaining 269 are PRE-EXISTING south-cluster / A2 body violations.
- Dead-code cleanup + per-segment validator caps both verified behaviour-neutral on
  CYXY (269/0 unchanged).

---

## OUTSTANDING TODOs (for the new session)

1. **COMMIT.** All of the above is UNCOMMITTED on `dev` (HEAD still `0844841`, the
   handover-#3 work). `git diff` is the whole refactor. Review + commit before more
   work. (Untracked `tools/trace_building_frontage.py` is pre-existing, not part of
   this.)

2. **Run / re-baseline the suite.** NOT run this session (mid-model, CYXY-focused per
   user). It will fail for OSM/painted-only airports until TODO 3, and the apt
   fixtures (SPJC/SPLP/HECA/MMOX) need re-checking — the connectivity grouping can
   shift rects vs the old per-name grouping (e.g. a degree-≥3 same-name node now splits
   where the old "≥2 NAMES" heuristic didn't). Verify each fixture; re-baseline genuine
   improvements (memory `byte_identity_vs_clean_architecture`).

3. **Convert the OSM/painted centerline producers to `TaxiCenterline`**
   (`pavement.centerlines._extract_osm_taxi_centerlines`, `apt_dat_reader.painted_
   taxi_centerlines`). They still return `(line, name)` tuples. apt airports work
   today (consumer helpers duck-type `.line`/`hasattr`), but a painted-only airport's
   `grade_graph.build_context` does `getattr(tcl, "line", tcl)` → would treat the
   tuple as a line and fail. Needed before those airports build.

4. **The 269 CYXY body violations** — the next grade target now the spine backbone is
   clean. Worst are the SOUTH cluster (junctions ~`(481,-504)`, `(-118,-468)`) and the
   A2-end apron `(-384,-270)`; these were in the 295 baseline (NOT caused by the
   refactor). Separate investigation, like handover #3's NEW-items list.

5. **Per-segment refinement loose ends** (low priority; CYXY uniform so no effect):
   - `taxi_routing` route-graph `edge_cap` still uses route-level `dominant_size()`;
     make per-segment if that graph is on an active path (it's likely legacy).
   - Validators using `dominant_size()` were converted; double-check no other
     route-level size lookup remains for a MIXED-width route.

6. **Carried over from handover #3, still open** (unaffected by this session):
   - Item 3 — anisotropic CURVE fix (`Allowance(cL,cT)`, supply real Δs∥/Δs⊥).
   - Item 4 — audit every check against principle #2 (map each to a `grade_law` rule
     or retire).
   - The building-anchored apron region > ~60 m from a building draping to DEM
     (handover #2/#3 follow-up) — re-check whether the connectivity fix changed it.

## Verification recipe
venv; `PYTHONHASHSEED=0 PYTHONPATH=src:.:tests`. CYXY spine/connectivity:
`build_airport_pavement("CYXY")` (NOT cached when iterating); `grade_graph_validate.
within_violations(L)` filtered to `v[4]` = spine; build `grade_graph.build_unified_
graph` and check a spine node exists at E (deg ≥ 2, ~U12↔F connected). Full suite
`venv/bin/python -m pytest tests/ -q` (will need TODO 3 + re-baseline first).
