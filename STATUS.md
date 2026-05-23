# Auto-Patch Status — geometry rework done; corridor-aware invariant cluster next

## TL;DR / current state

**HEAD = `04561aa`. Working tree CLEAN.** Suite = **15 failures**
(`venv/bin/python -m pytest tests/ -q`, ~4 min): the **9 pre-existing baseline**
(compare_target ×3, grade ×3, SPJC junction-invariants ×3) **+ 6 introduced by
this session's corridor-aware rect lengthening** (the open item below).

This session (`04561aa`) was a big **GEOMETRY rework** of the SPLP B-connector /
central-junction bug and the whole runway-segmentation path. The elevation
cascade (gated, `unified_jacobi`) is UNTOUCHED — still off, resume after geometry.

**The single best handover note is the memory file
`splp_b_connector_junction_rootcause.md`** — it has every fix, mechanism, and
dead-end from this session in detail. Read it first.

---

## DONE this session (commit `04561aa`) — the old STATUS geometry issues are FIXED

- **#1 B cross-connector + missing central junction (SPLP)** — FIXED. Root cause
  was NOT the diagonal-stub margin (old hypothesis) but the rect being built
  SYMMETRIC about an off-centre centerline. Fix: place taxi rects by the
  PAVEMENT — `rects._natural_half_widths_lr` + `half_left/half_right` in
  `_rect_from_axis_extended`. Plus corridor-aware chart-junction margin in
  `centerlines.py` (extend to the real widening; `CHART_JUNCTION_MARGIN_M` is now
  **15 m**, committed — the old "don't touch it globally" warning is superseded:
  it's safe now that rects are placed against the pavement).
- **#0 runway segmentation (CYXY + SPLP)** — REWORKED. Runway now splits at each
  pavement CONTACT's near/far edges (boundary-vs-runway-band arcs, proximity-based,
  one sub-rect per contact — SPLP 11→8, no tiny rects); splits BOTH runways at
  runway-runway crossings (CYXY `02/20+14R/32L`, `02/20+14L/32R`); cuts at profile
  PEAKS/VALLEYS for vertical-curve support (CYXY 14R/32L crest). Blast pads /
  displaced thresholds are part of the runway (contacts there cut; the threshold
  CUT itself comes from the segmenter's CIFP `anchored_t`). All in `pipeline.py` +
  `runway_segments.py`.
- **#2 tile-slice seam wedges** — FIXED. `tile_cut._absorb_seam_slivers` merges
  tiny seam wedges into the adjacent shape; rects STAY rects
  (`_extend_rect_over_sliver`); node_altitudes neighbours unioned with seam-pinned
  altitudes. Also `seam_anchors`: a taxi rect that only grazes/ends at a seam stays
  a 4-corner rect (defer to the slice) instead of becoming whole-rect node_altitudes
  (the "long taxiway A → node_altitudes" bug).
- **Conformance slivers** — `canonical_points.weld_layout_vertices` (fresh-registry
  weld before conformance) drove SPLP/SPJC crossings → 0.
- **stub↔junction triangular gap (SPLP node 20)** — FIXED in `junction_rules.py`:
  the Rule-2 sloping-edge snap no longer yanks a junction vertex that is already a
  shared rect corner.

---

## OPEN — the 6 corridor-aware invariant failures (NEXT TARGET)

The corridor-aware rect lengthening (it makes rects reach to the real pavement
widening) extends some rects into apron/junction-adjacent zones, tripping 6 tests:

| airport | failing tests |
|---|---|
| CYXY (stub G, ~57 m alongside apron #40) | `taxi_rects_not_alongside_apron`, `sloping_rect_slopes_only_along_axis`, `no_vertex_on_sloping_rect_flat_edge` |
| SPLP | `junction_vertices_have_source`, `junction_vertices_outside_pavement` |
| SPJC | `no_vertex_on_sloping_rect_flat_edge` |

Common thread: a longer/asymmetric rect has its sloping edge running alongside a
junction/apron (CYXY stub G is the clearest — a thin 75 m stub down the apron
flank). **Lever:** scope the corridor-aware extension so it stops where EITHER
side opens into apron/junction (don't run a rect alongside an apron — esp. stubs,
which inherently hug aprons), OR have the absorption pass trim such rects (note:
the long-edge-adjacent absorption EXEMPTS runway-corridor rects within 160 m of a
runway, so stub G slips through). The asymmetric-placement change alone was only
+1 failure; the corridor-aware lengthening added the other ~5.

The 9 PRE-EXISTING baseline failures are unrelated (compare_target fixtures need
re-cutting; grade is groundside/terminal-separation + the apron-side stub-A
holdout; SPJC junction drift) — see `suite_baseline_dev_head.md`.

---

## THE ELEVATION/GRADE WORK (done, GATED OFF; resume AFTER geometry)

Unchanged this session. `elevation_per_surface/unified_jacobi.py::solve` has the
priority cascade (seam/runway HARD → taxi → apron → terminal) + two flags, both
default `False`: `_PER_AXIS_JUNCTIONS` (pairs with `check_grade.run_checks(
taxi_axes_ll=…)` from `layout.apt_taxi_centerlines`) and `_USE_L2_FIT`
(`_compliant_spread_fit`). Flags ON clear SPLP −10025/−10026; holdout = apron-side
stub A (infeasible as anchored). Full design + dead-ends in
`project_solver_priority_cascade.md`.

---

## HOW TO TEST / GOTCHAS
- Build one airport: `from auto_patch.pipeline import build_airport_pavement;
  build_airport_pavement("SPLP", xplane_root(), compute_elevations=True)`
  (sys.path += `src/`, repo root, `tests/`; `from conftest import xplane_root`).
  **compute_elevations=True is REQUIRED** to exercise the runway segmenter +
  seam/tile_cut pipeline — `False` skips them (runways stay single rects).
- Full suite: `venv/bin/python -m pytest tests/ -q`. **Baseline now = 15.**
- **Circular import:** import `auto_patch.pipeline` BEFORE `auto_patch.junction_repair`
  (or monkeypatching it) — junction_repair ↔ elevation cycle.
- The user EDITS apt.dat + source files in parallel — re-check `git status`/`git log`
  before committing; commit ONLY your own files.
- Per-tile builds: SPLP is cut at the lon=−77 seam (x≈−137 in local m); shapes
  west of it are dropped (other tile) — the big "uncovered raw pavement" number is
  that, not a gap.

## Memory pointers
- `splp_b_connector_junction_rootcause.md` — **THE key note for this area**: every
  session-44 fix (asymmetric placement, weld, runway contact-arc split, peak/valley
  cuts, seam-wedge absorption, taxiway-A-rect, stub-gap) with mechanisms.
- `project_solver_priority_cascade.md` — the gated elevation cascade.
- `suite_baseline_dev_head.md` — the pre-existing failure set (now stale count: it
  says 9; current baseline incl. corridor-aware is 15).
