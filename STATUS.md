# Auto-Patch Status — solver cascade + per-axis grading (GATED); geometry-first next

## TL;DR / current state

**HEAD = `3e2e967`. Working tree CLEAN.** This is the known-good baseline:
the new solver (priority cascade + per-axis junction grading + closest-to-DEM
spread fit) is committed but **GATED OFF** — both flags default `False`, so the
solver behaves as the proven DEM-attraction relaxation. **Suite = 9 pre-existing
failures** (the cascade is non-regressing, verified twice).

**If CYXY (or anything) looks bad: it was the uncommitted
`CHART_JUNCTION_MARGIN_M` experiment, now REVERTED.** That value (25 m) is
GLOBAL and was tuned for CYXY's junction sizing; dropping it to 10/15 m shrank
junctions everywhere and produced bad CYXY elevations. It's back to 25 m. Don't
change it globally again (and note: it doesn't even affect cross-connectors —
see the geometry bug below).

**Next session: LOCK DOWN THE GEOMETRY before touching elevation/grading.** The
elevation work is done & gated; the remaining blockers are geometry
(rect↔junction connectivity), and they likely also gate the grade fix (a 1-node
join can't carry the grade spread across it).

This session's commits (all gated off / test-only / clearance is the user's):
- `a4e0bed` priority cascade; `2585844` per-axis grading (solver+audit);
  `e2cc12d` closest-to-DEM spread fit; `68bed11`+`3e2e967` rect-slope invariant test.

---

## GEOMETRY ISSUES TO FIX FIRST (this is the next session's job)

### 1. B cross-connector dead-ends with a gap to the junction (SPLP)
The B `cross_connector` shares only ~1 node (sometimes a several-metre GAP) with
its junction instead of sharing its full end edge (2 corners). Source pavement is
continuous (user confirmed in WED), so it's an emit-side gap.
- **Root cause (per user 2026-05-22): the cross-connector is getting the ~30 m
  DIAGONAL-STUB end margin, which only diagonals should get.** It ends ~30 m short
  of the junction node while the junction sits ≤ the chart margin from it → a gap
  the rect↔junction snap (`junction_rules._snap_junction_vertices_to_rect_flat_edge_corners`)
  can't close (no junction vertex near the connector's far end corner to snap to).
- Evidence: the connector's corners are IDENTICAL at `CHART_JUNCTION_MARGIN_M`
  10 vs 15 → its length is governed by the diagonal margin, not the chart margin.
- **Where:** `pavement/centerlines.py::_rect_margin_frac_for` (line ~1049). The
  `20° < perp_diff < 75°` branch returns 0.30 (the 30 m-ish diagonal margin) when
  an endpoint is within `STUB_ENDPOINT_RUNWAY_M = 80 m` of a runway. The B
  connector's ends are ~195 m from the runway, so it SHOULD fall through to the
  0.15 default — but it behaves like it gets the big margin. **NEXT STEP: instrument
  `_rect_margin_frac_for` for the B centerline (print `perp_diff`, the branch
  taken, `ep0_near`/`ep1_near`, the returned frac) to see exactly why it gets the
  large margin, then ensure cross-connectors (taxiway↔taxiway, no runway endpoint)
  get the normal margin.** Fix should generalise (user: "so it doesn't happen
  somewhere else").

### 0. CYXY runway segmentation (user flagged — the main CYXY issue)
User reports the main CYXY problem is runway SEGMENTATION. This is upstream
GEOMETRY (`runway_segments.py` / `runway_redistribute.py`), run BEFORE the solver.
**NOT from this session's elevation work** (cascade/per-axis/spread are gated off;
runways are HARD anchors unchanged). So it's either PRE-EXISTING (baseline already
fails `grade[CYXY]` + `neighbour_corners[CYXY]`, tied in memory to runway
threshold-vs-DEM reconciliation, commit dd04d8e) OR from the parallel clearance/RESA
work (`474af1d`/`8087ff1`, "RESA builder off the runway-end pavement edge" — touches
runway-end geometry). NEXT: build CYXY, inspect runway sub-rects (count/shape/elev),
diff against pre-clearance-commit behaviour to localise.  [SYMPTOM: to be filled in
by user — too many segments? gaps between sub-rects? wrong elevations? at the ends?]

### 2. Southern stub A = two rects that should be merged after the slice (SPLP)
The tile slice (lon=−77 seam) splits the southern stub A into a hi/lo body
(ctr ≈ (−124,−126), 62.4/62.2) + a thin `node_altitudes` boundary strip
(ctr ≈ (−130,−124)) that share an edge. They should merge into one rect.
A tile_cut post-merge issue.

### NOTE: the slope direction was NEVER a bug
The apron-side stub A (way `-10005` in the per-tile patch; hi/lo 72.0/71.6; bbox
ll −12.1560322,−76.9980678 .. −12.1557541,−76.998327) was thought to "slope
perpendicular/parallel to the runway." It does NOT — it slopes ~perpendicular to
the runway, ALONG its centerline (correct). The "49°" I reported was a measurement
bug (computed runway direction from a skewed runway SUB-RECT; use the runway's full
south↔north long axis ≈ azimuth 18°). It's a wide pad (28.4 m across × 22.8 m along
its centerline), which is allowed. Don't chase the slope.

### NOTE: determinism is fine
Cross-run geometry differences I saw were the user editing the apt.dat between
builds, NOT non-determinism (two builds in one process are byte-identical). The
apt.dat WAS edited during this session (junction count dropped ~18 → 2 in late
builds) — the new session should rebuild fresh and not assume my mid-session
coordinates.

---

## THE ELEVATION/GRADE WORK (done, gated; resume AFTER geometry)

### The cascade (`elevation_per_surface/unified_jacobi.py::solve`)
Ordered priority cascade (user: "grade is sacred; the cascade order decides who
yields"): seam/runway (HARD) → TAXI (rects+junctions) → APRON → TERMINAL. Each tier
solved against the frozen tier above; node OWNER tier = highest-priority role using
it; tiers couple via shared HARD nodes, not cross-tier edges. Helpers `_node_tiers`,
`_run_phase`, `_TIER_*`.

### Two gating flags (both default False)
- `_PER_AXIS_JUNCTIONS` — junction edges = ring + along-centerline only; the
  unregulated inter-centerline diagonal is dropped. **PAIRS with the audit**:
  `check_grade.run_checks(taxi_axes_ll=…)` grades junctions per-axis using the
  build's APT.DAT centerlines (`layout.apt_taxi_centerlines`, passed lat/lon —
  NEVER re-derived from OSM). The grade test feeds `taxi_axes_ll` only when this
  flag is on, so solver+audit flip together.
- `_USE_L2_FIT` — taxi/apron phases use `_compliant_spread_fit` (over-relaxed cap
  projection to FULL grade compliance, `_SPREAD_OMEGA=1.0`; SOR ω>1 diverges) —
  the "follow DEM, clamp to grade, propagate to smooth" fit. The DEM is a
  PREFERENCE, not a constraint.

### Validated result (flags ON)
Per-axis + spread CLEARS SPLP −10025/−10026. Lone holdout = the apron-side stub A
(infeasible as currently anchored, ~2.0%). **Likely gated by the GEOMETRY bug**:
the 1-node connector joins fragment the network, so the spread can't propagate the
descent down the available southern-distance-to-the-seam. Fix geometry (#1/#2),
then re-test — the grade may follow. If still short, the elongation lever (give the
descent more clean rect-length) needs a TARGETED mechanism (cross-connector margin
fix, NOT the global `CHART_JUNCTION_MARGIN_M`).

---

## HOW TO TEST / GOTCHAS
- Build one airport: `from auto_patch.pipeline import build_airport_pavement;
  build_airport_pavement("CYXY", xplane_root(), compute_elevations=True)`
  (sys.path += `src/`, repo root, `tests/`; `from conftest import xplane_root`).
- Flip flags for an experiment (do NOT commit on): `from
  auto_patch.elevation_per_surface import unified_jacobi as uj;
  uj._PER_AXIS_JUNCTIONS=True; uj._USE_L2_FIT=True` BEFORE building; pass
  `taxi_axes_ll` to `check_grade.run_checks` (built from `layout.apt_taxi_centerlines`
  + `apt_taxi_letters`, see `tests/test_pavement_grade.py`).
- Full suite: `venv/bin/python -m pytest tests/ -q` (~3-5 min). Baseline = 9.
- The user EDITS apt.dat + source files in parallel — re-check `git status`/`git log`
  before committing; commit ONLY your own files (this session: only unified_jacobi.py,
  check_grade.py, the two test files; clearance.py + config.py are the user's).
- Diagnostics in `tools/`: `diag_splp_*.py`, `verify_splp_grade.py`. `/tmp/*.py` were
  scratch (gone on reboot).

## Memory pointers
- `project_solver_priority_cascade.md` — full cascade/per-axis/spread design, every
  dead-end, the stub-A + connectivity analysis. THE key note.
- `suite_baseline_dev_head.md` — the 9-failure set.
