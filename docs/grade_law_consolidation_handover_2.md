# grade_law consolidation — handover #2 (continues `grade_law_consolidation_handover.md`)

> ⚠ **SUPERSEDED (2026-06-30 audit) by `grade_law_consolidation_handover_4.md`.**
> Carry-forward open items (route-band-on-OSM; building-anchored apron >60 m draping to
> DEM) → **`OPEN_ITEMS.md`**.

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
| `defb96e` | **Item 5 — route_field cleanup.** Removed dead `route_ctx` plumbing (param + `route_ctx_from_layout` + callers), the `_rf_*` collection in `elevation.py`, and `ROUTE_NOISE_FRAC` (imported never used). Repointed `grade_feasibility_audit._route_band_intervals` off the deleted `route_field` onto the unified band (`reach_band_unified`; SPJC route-bounded reps 0→1671). Behaviour-neutral (affected-file subset = same baseline failures). ⚠ `ROUTE_FIELD_MODEL`/`ROUTE_FIELD_LOCAL_WINDOW_M` are NOT dead (live within-shape window law) — kept; handover #1/#2 mislabelled them. |
| `10ab6d5` | **Item 2 PART 1 — edge-skeleton reach for no-centerline pavement** (gate `O4_SKELETON_REACH` ON). CYXY west apron (65 893 m²) fed by DISCOVERED taxiways (ref `TX*`, on `layout._discovered_centerlines`, not `apt_taxi_centerlines`) was a reach ISLAND → no band → feeders 16 m apart in the bowl. `building_feasibility._build_skeleton_band` = a fallback reach over the WELDED EDGE skeleton (`G.edges`) ∪ `spine_adj`, anchored at `G.runway_anchor` ∪ runway-coincident nodes, used ONLY where the centerline band is None. West apron banded, feeders converge, route_reach 3→2, suite = baseline. Centerline spine stays primary (curves + item-3 anisotropy). |
| `fbc7e2b`,`3869756` | **Item 2 PART 2 — feeder convergence (TILT model)** (gate `O4_NOBUILD_APRON_SEAT` ON; `3869756` is the working version, `fbc7e2b` the gated-off flat predecessor). `anchors.build_nobuilding_apron_seats` + `_project_apron_contacts`: a no-building apron is ANCHORED like a building (spines grade to it) but at PER-CONTACT feasible levels — the apron TILTS ≤cap between feeders via a POCS projection `min Σ(L_i−t_i)² s.t. |L_i−L_j|≤cap·d_ij, f_i≤L_i≤ce_i`. Clears `route_reach` to 0 at CYXY; `test_cyxy_route_reach_zero` GREEN; suite = **19** baseline, no regressions. The anti-gaming guard is now SYNTHETIC. |

State: suite **19** (full-suite-verified at `3869756`, IDENTICAL to the pre-item-2
baseline set — items 5 + 2 added no regressions and flipped `test_cyxy_route_reach_zero`
green). Baseline set saved this session; route_reach is now a hard gate.

UNCOMMITTED (working tree, gated OFF — a partial step toward the runway-anchor fix
below, NOT the fix): `config.BUILDING_SPINE_LIFT_CORRIDOR_M` + `_spine_floor_per_node`
using it under gate `O4_LONG_SPINE_LIFT` (default 0). It extends the building→spine
lift past the 200 m frontage corridor (building22 is 219 m from its spine), but the
lift is built on an over-credited ceiling (see below), so it doesn't fix building22
alone. Keep or revert when the runway-anchor work lands.

Tooling: `tools/trace_reach_route.py` PORTED to the unified band
(`reach_band_unified(layout,G)` + spine path reconstruction; reports serving
centerline, perp on-pavement fraction = phantom-across-grass detector, 2nd-nearest
route). `tools/trace_building_frontage.py` is still STALE (same retired imports /
3-tuple `reach_band_for`) — port it the same way if needed. (It's untracked; leave
or port.)

Relevant memory written this session: `splp_seam_cliff_fix`,
`cyxy_route_band_ceil_rootcause`, and the corrected `splp_seam_apron_polish`
(seam → SMOOTHED DEM, never raw HGT).

## ★★ TOP-PRIORITY NEW FINDING — RUNWAY-ANCHOR COVERAGE BUG (critical to the entire solve)

**Getting the runway anchors right is foundational: every reach band, building seat,
spine lift and route-band is measured as a cap-distance FROM the runway anchors. A
missing or mis-placed anchor inflates the route to a node, which over-credits its
ceiling, which strands buildings/aprons above their real reach.** This was traced
end-to-end at CYXY building22 (a building seated at 702.2 that the apron can't grade
to — the 116 607 m² north apron's 15.5% cliff at (60.7188,-135.0781), inside the
baseline `test_pavement_grade[CYXY]`).

THE BUG (source-confirmed): in the **raw apt.dat** routing graph (`apt.taxi_nodes`/
`apt.taxi_edges`, 1 connected component), the unnamed apron route **~U12** and
taxiway **A** share a node at local **(-477,862)**, and from there it is **142 m via
A** (2 A edges) to a runway contact at **(-345,914)**. Our unified spine graph keeps
the A↔~U12 weld (node 658 at (-477,862) is on 3 centerlines, 3 spine neighbours) —
but the cap-route from node 658 to a runway anchor is **~740 m** (or 620 m via E),
NOT 142 m. Reason: **A's near runway contact at (-345,914) is NOT a `G.runway_anchor`**
— `_runway_anchors` (`grade_graph.py`) anchored only A's FAR contact (-162,978, via
anchor 357), so reach detours to the far anchor. That inflated route is the whole
over-credit: building22's frontage ceiling comes out **705.8** (via ~U12's 620 m) when
the true reach via A's 142 m route is **≈ 694 + 1.5%·142 + 1%·215 ≈ 698** — i.e.
building22 should be capped ~698 and the apron would grade, instead of being seated at
its 702 DEM and stranding the apron.

WHY `_runway_anchors` misses (-345,914) is the one thing left to pin down: it anchors
apt.dat centerline ENDPOINTS within `_CONTACT_M`=12 m of a runway, then the nearest
emitted node within `_NEAR_M`=18 m. (-345,914) is an endpoint of two A segments at
0 m from a runway, so it *should* qualify — verify whether (a) no emitted node sits
within 18 m there, (b) `_sample_runway_segment_elev` returns None, or (c) it's a
runway_crossing handled differently. The fix is to ensure EVERY taxiway↔runway
contact that exists in raw apt.dat becomes a runway anchor (mid-network joins, not
just the two centerline endpoints).

This is the SAME ROOT FAMILY as item 2 PART 1 (no-centerline islands) and the user's
shape-107 (an apron mis-classified mid-taxiway-F, breaking F's spine, shapeID 107 =
the 640 m² apron at (60.7186,-135.0771)): the spine/reach graph not faithfully
reflecting source connectivity. After the anchors are right, re-evaluate whether the
long-range spine lift (`O4_LONG_SPINE_LIFT`, gated off) is still needed.

How to reproduce / verify (probes used, all `cached_airport_layout("CYXY")`):
- raw graph: `apt = apt_dat_reader.load_airport(_pick_best_apt_dat_against_osm(...))`;
  `unnamed_edge_component_names(apt)` gives the `~U` labels; Dijkstra over
  `apt.taxi_edges` (geometric weights) ~U12-near-b22 → nearest runway-contact node =
  **142 m via A**.
- our graph: `G = build_unified_graph(L, b2i)`; cap-Dijkstra over `G.spine_adj` from
  `G.runway_anchor` to node 658 = **740 m**; `reach_band_unified(L,G)` at building22
  centroid = (682.8, **705.8**). ⚠ Build G from the SAME (pre-emit) layout the solve
  uses — rebuilding from a post-build layout can differ; here it MATCHED the live
  solve (verified by an `O4_B22_DEBUG` print), so the over-credit is real, not a
  probe artifact.

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

**PART 2 — feeder CONVERGENCE (DONE, gate `O4_NOBUILD_APRON_SEAT` default ON; suite
== 19 baseline, no regressions; `test_cyxy_route_reach_zero` GREEN).** The TILT
model (user 2026-06-28): a no-building apron is ANCHORED like a building so its
feeder SPINES grade to meet it, but at PER-CONTACT feasible levels (the apron tilts
≤cap between feeders) — NOT one flat level. `anchors.build_nobuilding_apron_seats`:
for each no-building apron, take each feeder's contact (nearest route vertex) with
its reach band + DEM-biased target `t_i = clamp(DEM_i, band_i)`, then
`_project_apron_contacts` solves the metric/Lipschitz projection
`min Σ(L_i−t_i)² s.t. |L_i−L_j| ≤ cap·d_ij, f_i ≤ L_i ≤ ce_i` by cyclic POCS. A
solution clears `route_reach` BY CONSTRUCTION (its condition IS the constraint set);
an EMPTY polytope = FUNDAMENTAL → skipped. `solve.py` merges the result into
`building_seats` (HARD anchor → spines adjust). KEY: per-contact tilt levels are
each in-band, so the spine reaches them without an over-cap step — that's why the
TILT version is clean where the earlier FLAT version (one level for all feeders)
forced unreachable levels and regressed `cyxy_spine_zero`/`_no_bowl` + HECA runway.
`t_i = clamp(DEM, band)` also pulls a feeder floating ABOVE its band (CYXY 280 m²
junction at 714.7, ceiling 712.79) back into reach. Anti-gaming guard
`test_route_reach_detects_incompatible_apron` is SYNTHETIC (gate-independent).
The route-band `pinned` class (empty band) is the per-vertex cousin — HECA ~1,300
(multi-runway, fundamental).

OPEN follow-up (user 2026-06-28, separate pre-existing issue inside
`test_pavement_grade[CYXY]`): a BUILDING-anchored apron's region MORE THAN ~60 m
from any building has no grade enforcement and drapes to DEM — CYXY's 116 607 m²
north apron @(60.7188,-135.0781) hits 698.7 next to 690 (15.5% over 56 m). The
≤cap edge EXISTS (budget 0.56 m) but the violation survives feasibility — the north
boundary node is raised to DEM by a POST-feasibility step (ribbon-seam / boundary→
DEM adoption). Fix = grade-enforce the far-from-building apron interior (re-project
after the boundary/ribbon adoption, or hold those boundary nodes into band).

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
