# grade_law consolidation — handover #2 (continues `grade_law_consolidation_handover.md`)

Read `docs/grade_law_consolidation_handover.md` first (the original goal +
architecture + principles). This file records what the **2026-06-28 session**
landed and what is still open. The goal is unchanged:

> ONE canonical ruleset (`grade_law`) that BOTH the solver and the validators use,
> so output is tuned by editing *rules*, not by chasing implementations. No airport
> is legitimately infeasible — every violation is a solver bug, a missing rule, or
> a rule needing adjustment; surface it, never hide it.

## DONE this session (committed on `dev`)

| commit | what |
|---|---|
| `6e6f133` | **Original item 1 — route-band CONFIRMATION on the one graph G.** `grade_graph_validate.route_band_violations(layout)` builds G (`_build_node_list`+`build_unified_graph`), gets `band = reach_band_unified(layout, G)`, and checks every airside taxi/apron/junction/building vertex's solved elev ∈ `band(x,y)`. Reports 3 classes, none dropped: `ceil` (above reach), `floor` (below reach), `pinned` (EMPTY band, floor>ceiling = fundamental). WARN re-wired in `elevation.py`. `tests/test_route_band.py`: SPJC gates HARD at 0; CYXY/SPLP/HECA xfail-tracked; + 2 anti-gaming guards. |
| `7783d66` | **ecap fix in `reach_band_unified`.** The foot-climb cap was read off the `G.spine_adj` edge between the foot's two bracketing spine nodes and fell back to `TAXI_MAX_GRADE` (1.5%) whenever they weren't a DIRECT edge (any long centerline segment) — so a code-B (3%) taxiway was credited 3% at one point and 1.5% 60 m further. Now credits the SERVING centerline's own per-letter cap. **Flipped `test_cyxy_spine_zero_no_bowl` GREEN** (building16/19 were bowled by that under-credit); suite 21→20. |
| `2ec5146` | **Building-size reach rule is canonical.** `grade_law.building_requires_full_frontage(area)` (the `<2000 m²` central-chord vs `≥2000 m²` full-frontage decision), consumed by BOTH `build_building_seats` and `route_band_violations`. The checker treats a SMALL building pad as a LOCAL reach anchor (apron grades from it at the apron cap over an on-pavement chord), so its looser non-central frontage isn't false-flagged. CYXY route-band 150→106 (building false-positives 27→0). |
| `a7b0e42` | **SPLP cross-tile seam cliff fixed (3 bugs).** (1) seam pinned to RAW HGT (`alt_strict`, nodata at the tile edge) → SMOOTHED DEM (`_sample_dem`); (2) SOFT junction/apron seam vertices were never hard-pinned (the seam blocks only processed shapes with pre-set `node_altitudes`) → `one_profile_solve` raised them; `_seed_elevations` now hard-pins EVERY seam-key vertex by position to the smoothed DEM; (3) the spine had NO anchor at the seam (`SEAM_FIELD_ANCHORS` imported-but-unused, `_seam_cut_lines` set-but-unconsumed) → added `route_profile.solve._seam_spine_anchors` (centerline×seam crossing → hard spine anchor) so the spine SPREADS the route→seam drop. **`tile_cut_parity@SPLP` GREEN** (SPLP 7→6); tile-77 seam region within-shape→0. Seam-gated ⇒ CYXY/SPJC/HECA byte-identical. |

State: suite ~21→**19** (derived: full-suite-verified at 20 after the ecap/small-
building commits, then seam-gated SPLP-only change fixed `tile_cut_parity@SPLP`).
**Run the full suite once to confirm 19 and re-capture the baseline set** (the old
`/tmp/suite_ab/clean.set` is the pre-session 21).

Tooling: `tools/trace_reach_route.py` PORTED to the unified band
(`reach_band_unified(layout,G)` + spine path reconstruction; reports serving
centerline, perp on-pavement fraction = phantom-across-grass detector, 2nd-nearest
route). `tools/trace_building_frontage.py` is still STALE (same retired imports /
3-tuple `reach_band_for`) — port it the same way if needed. (It's untracked; leave
or port.)

Relevant memory written this session: `splp_seam_cliff_fix`,
`cyxy_route_band_ceil_rootcause`, and the corrected `splp_seam_apron_polish`
(seam → SMOOTHED DEM, never raw HGT).

## REMAINING (original handover items, updated)

### Original item 1 — route-band confirmation ✅ DONE (`6e6f133`)
Follow-up still open: it runs IN-MEMORY on the whole-airport layout (rebuilds G).
The "purist" OSM-path (reconstruct G from the shipped per-tile patch) is not done.
Also re-baseline `test_pavement_grade` route-band counts if you fold the band check
into the OSM path.

### Original item 2 — apron FEEDER-REACH rule — PART 1 (CONNECTIVITY) DONE; PART 2 (CONVERGENCE) OPEN
Item 2 splits into two distinct sub-problems, root-caused this session via CYXY's
65 893 m² west apron (the `test_route_reach` example):

**PART 1 — reach CONNECTIVITY (DONE, gate `O4_SKELETON_REACH` default ON).**
The west apron's feeder taxiways are DISCOVERED (synthesised from pavement, ref
`TX*`, no apt.dat centerline) — they live on `layout._discovered_centerlines`, NOT
`apt_taxi_centerlines`, so `_build_global_spine` / `_runway_anchors` (centerline-
based) never gave them a spine or a runway anchor. Result: the whole west complex
(apron + TX1–TX4 + local rects, ~113 nodes incl. runway segments) was a graph
ISLAND disconnected from every runway anchor → `reach_band_unified` returned `None`
there → no band → feeders unconstrained → they landed 16 m apart in the bowl
(684/689/674) → `route_reach` flagged. The full pavement EDGE graph didn't reach it
either, because the only bridge to the runway is a runway CROSSING (runways carry no
spine edges and only centerline-endpoint anchors).
FIX (`building_feasibility._build_skeleton_band`): a SECOND reach over the WELDED
EDGE SKELETON (`G.edges` — abutting shapes share exact node indices, no perp
tolerance) ∪ `spine_adj`, anchored at `G.runway_anchor` ∪ every pavement node
COINCIDENT with a runway segment (captures crossings the centerline-endpoint anchor
misses: 6→90 anchors at CYXY). `reach_band_unified.band()` uses it ONLY as a
FALLBACK where the centerline path returns `None` (the islands) — so centerlined
airports are byte-identical AND the smooth centerline spine stays primary for
curving taxiways (and is REQUIRED for item 3's anisotropic `Allowance`: the
centerline gives the longitudinal reference an edge-skeleton has no direction for).
RESULT: west apron gets band (692,699); feeders converge to 693/694/693;
`route_reach` 3→2. Full suite = 19 baseline, no regressions (only the anti-gaming
guard fired, because the west apron is genuinely fixed — re-pointed to the remaining
640 m² apron at (-530,1006)).
- ⚠ Design note (USER-confirmed): edge-skeleton ONLY where no centerline.
  Distance over a chord/edge graph is NOT route-faithful (it under-measures vs the
  curving route — CYXY served-node ceilings up to 24 m tighter), so it must NOT
  replace a real centerline spine. For a FEASIBILITY band this chord distance is
  actually the *rigorous* tightest-constraint bound; sparse "port" skeletons were
  MEASURED WORSE (tighter + less coverage — port-to-port diagonals are also
  short-circuits). Route-faithful distance fundamentally requires a centerline arc.

**PART 2 — feeder CONVERGENCE (BUILT, gate `O4_NOBUILD_APRON_SEAT` default OFF —
proven to work but over-constrains; needs refinement).** The 2 remaining flagged
aprons (small, no-building: 640 m² @(-530,1006), 280 m² @(-321,-422)) are NOT
connectivity cases — they HAVE bands, with NON-EMPTY feeder-band intersections (a
common level exists), but the solver leaves each feeder at its own DEM-driven level.
`anchors.build_nobuilding_apron_seats` (parallel to `build_building_seats`, wired in
`solve.py` and merged into `building_seats` after `building_spine_floor`) seats each
no-building apron FLAT at L = clamp(DEM, ∩ ring bands), so its welded feeder-contact
nodes go to L and the feeders converge. WITH THE GATE ON: `route_reach` → 0 at CYXY
(west feeders all 694.2, the 640 m² both 689.7), `test_cyxy_route_reach_zero` XPASS.
BUT the FLAT/HARD whole-ring seat over-constrains — as a heaviest anchor it fights
the spine/runway anchors and **regresses 3 suite tests** (`test_cyxy_spine_zero` +
`..._no_bowl`, HECA `runway_longitudinal_grade`) → gated OFF (suite back to 19).
REFINE before enabling: (a) make it a SOFT per-node FLOOR (tighten `node_band`
toward L) instead of a hard `building_seats` merge — this is the "Applied below as a
per-node FLOOR" the `solve.py:77` comment always intended; and/or (b) seat ONLY the
genuinely-incompatible aprons (the band already converges the west apron — don't
seat it) and SKIP apron nodes already owned by the spine/runway. Anti-gaming guard
`test_route_reach_detects_incompatible_apron` is now SYNTHETIC (gate-independent), so
it stays valid no matter how the solver evolves. The route-band `pinned` class
(empty band) is the per-vertex cousin — HECA ~1,300 (multi-runway, fundamental).

### Original item 3 — anisotropic CURVE FIX — STILL OPEN (untouched)
Plumbing is in (`Allowance(cL,cT)`, edges carry it). Supply real Δs∥/Δs⊥ per edge
(project onto the local spine; the hard part is the longitudinal-reference in
multi-branch junctions) and flip junction/curve edges to anisotropic. See
`docs/m4_constraint_graph_findings.md`.

### Original item 4 — audit EVERY check against principle #2 — STILL OPEN
`check_grade._check_plane_gradient` (test-only, no solver counterpart),
cross-shape proximity / vertex-to-edge / edge-midpoint (weld-invariant
confirmations), runway longitudinal + vertical-curve (build profile vs check are
separate code). Map each to a `grade_law` rule or retire/document.

### Original item 5 — cleanup from the route_field retirements — MOSTLY DONE
Landed this session (behaviour-neutral; affected-file subset = same 6 baseline
failures, no new/no fixed):
- ✅ `route_ctx` plumbing REMOVED end-to-end: `check_grade.run_checks` param +
  docstring, `verification.route_ctx_from_layout` + its call site, the
  `route_ctx=` arg in `test_pavement_grade`, and the `route_ctx_from_layout`
  caller in `grade_feasibility_audit`.
- ✅ `elevation.py` `_rf_runway_rings/_rf_check_pts/_rf_check_src` (+ `_rf_groundside`)
  dead collection REMOVED.
- ✅ `ROUTE_NOISE_FRAC` RETIRED (was imported in `elevation.py` + `solver_primitives.py`
  but never used; the route_field band that consumed it is gone) — constant +
  `__all__` + both imports + `check_grade` import/fallback + the stale formula
  reference in `config.py`'s ROUTE-FIELD comment.
- ✅ `grade_feasibility_audit._route_band_intervals` REPOINTED off the deleted
  `route_field` onto the unified band: builds `G` (`_build_node_list` +
  `build_unified_graph`) and reads `building_feasibility.reach_band_unified` per
  regulated airside node, querying by position in the layout's OWN anchor frame
  (`layout.ll_to_m`, NOT the audit's mean-centred frame). Verified on SPJC:
  route-bounded reps 0→1671, bands sane, no frame error.
- ⚠ CORRECTION to handover #1/#2: `ROUTE_FIELD_MODEL` and
  `ROUTE_FIELD_LOCAL_WINDOW_M` are NOT dead — they are the LIVE within-shape
  local-window grade law (pairs > window not graded against each other; the
  route-band is the long-range law). Used in `elevation.py`
  `_report_within_shape_violations`, `solver_primitives.constraints_from_pavement`,
  and `check_grade._check_within_shape`. KEPT. (`ROUTE_FIELD_MODEL` is an
  always-True model flag; inlining it is a separate optional simplification, not
  dead-code removal — left as-is so the windowing model stays self-documenting.)

Still open under item 5:
- `Allowance.flat_cap()` asserts `is_flat`; once item 3 makes rules anisotropic,
  the `%`-report sites need an anisotropic-aware report. (Blocked on item 3.)

## NEW open items surfaced this session (all xfail-tracked, NOT regressions)

- **CYXY route-band south cluster (~106 ceil)**: a DIFFERENT cluster from the fixed
  A2-end — junctions ~715 m near local `(-300,-450)`, NOT building-pinned. Own
  investigation. (`test_route_band_zero[CYXY]` xfail.)
- **SPLP `pavement_grade[SPLP]` (~10 within-shape)**: a WEST-side region
  (building10 / apron `-10037` near local `(-423,-483)`), pre-existing, unrelated
  to the now-fixed seam. Plus the SPLP route-band `floor` set (taxi/junction below
  the runway-reach floor) — likely the same "network doesn't descend" family as the
  seam was, but away from a seam (no spine seam-anchor to lean on); needs the
  route-profile to trend toward local terrain.
- **HECA route-band**: ~395 feasible + ~1,300 `pinned` (multi-runway fundamental) —
  the big item-2 feeder-reach case. (`test_route_band_zero[HECA]` xfail.)
- Drive `test_route_band_zero[CYXY/SPLP/HECA]` to XPASS as the above land (the gate
  flips automatically; SPJC already hard-gates at 0).

## Verification recipe (unchanged from handover #1)
- venv only; `PYTHONHASHSEED=0 PYTHONPATH=src:.:tests`. Full suite
  `venv/bin/python -m pytest tests/ -q` (~6–11 min, xdist).
- **Only SPLP has a tile seam** among the fixtures (CYXY/SPJC/HECA single-tile) —
  for seam work, test SPLP per-tile: `tests/test_tile_cut_parity.py` +
  `test_pavement_grade.py::test_pavement_grade[SPLP]` (build per-tile via
  `cached_airport_layout("SPLP", tile_lat=-13, tile_lon=-77 or -78)`).
- Tools: `tools/diff_constraint_graphs.py ICAO`, `tools/grade_feasibility_audit.py
  ICAO`, `tools/check_grade.py ICAO`, `tools/trace_reach_route.py ICAO --coord=x,y`
  (ported), `tools/probe_spine_grade.py`.
- `route_band_violations(layout)` is the fast in-memory band check; build any
  airport with `from conftest import cached_airport_layout`.
- Builds are DETERMINISTIC with `PYTHONHASHSEED=0`; run-to-run drift is a bug, not
  inherent (memory `nondeterminism-cause`).

## Hard-won lessons this session (read before diving)
- The route-band check / building seats / spine solve all consume ONE band
  (`reach_band_unified`). A discrepancy between "where we BUILD" and "where we
  CHECK" is a bug IN that one band, not two graphs — verify by tracing the band at
  the specific point, not by reasoning.
- To find WHERE a node's elevation goes wrong, instrument the solve directly
  (env-gated print of `elev[i]`+`hard` after each stage: `_seed_elevations` →
  `_solve_spine_profile` → `one_profile_solve` → `feasibility_project`). The SPLP
  seam root cause (soft vertex not hard-pinned, raised by the body fill) was only
  findable that way.
- The spine solve spreads grade between SPINE anchors only. A node that should
  descend (e.g. to a seam/terrain pin) but is a BODY node won't — it must be a
  spine anchor. (User's framing: "shouldn't the spine spread the grade between
  anchors?")
- Seam pins to the SMOOTHED DEM, NEVER raw HGT (`alt_strict` returns nodata at the
  tile edge).
