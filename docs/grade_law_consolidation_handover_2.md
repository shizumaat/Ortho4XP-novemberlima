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

### Original item 2 — apron FEEDER-REACH rule in grade_law — STILL OPEN
`route_reach_violations` surfaces it; `test_cyxy_route_reach_zero` is xfail (solver
doesn't yet converge incompatible feeders). DEFINE the rule (feeders — taxiways AND
abutting building pads — converge to a shared reachable level) in `grade_law`, have
the SOLVER apply it, gate it. Split feasible (gate) from fundamental (documented
transition). Note: the route-band `pinned` class (empty band) is the per-vertex
cousin of this — HECA has ~1,300 pinned (multi-runway).

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

### Original item 5 — cleanup from the route_field retirements — STILL OPEN
- `route_ctx` plumbing is dead: `check_grade.run_checks` `route_ctx=` param,
  `verification.route_ctx_from_layout`, the `route_ctx=` arg in
  `test_pavement_grade`. Remove. (`tests/test_pavement_grade.py` still builds + passes
  `route_ctx` though `check_grade` ignores it.)
- `elevation.py` `_rf_runway_rings/_rf_check_pts/_rf_check_src` dead collection.
- Config gates likely dead post-route_field: `ROUTE_FIELD_MODEL`,
  `ROUTE_FIELD_LOCAL_WINDOW_M`, `ROUTE_NOISE_FRAC` (verify then retire).
- `grade_feasibility_audit._route_band_intervals` degrades to `{}` (guarded import
  of the deleted `route_field`) — repoint to `route_band_violations` / band-on-G.
- `Allowance.flat_cap()` asserts `is_flat`; once item 3 makes rules anisotropic,
  the `%`-report sites need an anisotropic-aware report.

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
