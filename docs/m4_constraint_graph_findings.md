# M4 findings — the two within-shape pair generators disagree TWO ways

> ✅ **RESOLVED (2026-06-30 audit).** The hand-back below was later acted on: the two
> generators were unified onto one shared law. `tools/check_grade.py` now consumes
> `grade_graph.shape_constraints` → `grade_law.classify_pair` directly (commit `e22e39e`
> "check_grade reads the shared grade law" + the grade_law one-plane-rule work `0bbb097`,
> `3124a2a`), so it no longer reimplements its own pair selection. The original status
> line is preserved below for history.

**Status (original — now superseded): HANDED BACK for a human modeling decision** (per the M4 NOTE in
`docs/cleanup_consolidation_plan.md`: "this changes *what is counted* … if unsure,
STOP and leave a written summary instead of guessing"). The reusable measurement
tool `tools/diff_constraint_graphs.py` and the data below are the deliverable; the
narrowing itself is NOT applied, because it is not the mechanical one-way narrowing
the plan assumed.

## What M4 assumed
> "Today `check_grade.iter_shape_grade_constraints` is an independent
> reimplementation of `grade_graph.shape_constraints`; they cross-disagree … The
> B-only excess is long cross-apron diagonals (median 360m) the solver decouples
> on purpose. **Decision: NARROW check_grade to the model.**"

This frames B (check_grade) as a *superset* of A (grade_graph) — drop B's extra
long apron chords and the rulers agree.

## What the measurement actually shows
`tools/diff_constraint_graphs.py` keys every soft-airside (apron/junction) pair
by its endpoints' lat/lon (frame-independent) and diffs the two generators:

| fixture | \|A\| model | \|B\| test | A∩B | A∖B (model-only) | B∖A (test-only) | **A△B ≤60m** |
|---|---|---|---|---|---|---|
| OEMA | 26656 | 27860 | 10737 | 15919 (junction 15887) | 17123 (apron 15692) | **7964** |
| CYXY | 10195 | 12643 |  4831 |  5364 (junction 5289)  |  7812 (apron 6257)  | **4251** |

The disagreement is **two-way and structural**, not a one-way superset:

1. **B∖A — apron (mostly >60 m).** check_grade keeps long apron body↔body chords;
   grade_graph drops them via `_APRON_BODY_CHORD_MAX_M`. This is the part M4
   assumed — and it is mostly >60 m, so it does NOT affect the ≤60 m acceptance.

2. **A∖B — junction (≈half ≤60 m).** This is the dominant ≤60 m term and the
   plan did not anticipate it. `grade_graph.shape_constraints` regulates junction
   cross-axis diagonals at the uniform `body_cap` (1.5 %). `check_grade`'s
   per-axis junction rule (`_per_axis_allowance`, with `taxi_axes`) treats a pair
   off every shared lane as an **unregulated cross-axis diagonal and SKIPS it**
   (`if allowance is None and role == "junction": continue`). So the two encode
   *different physical models of junction grading*: uniform-visibility (A) vs
   per-axis-with-diagonal-skip (B).

## Why this is a modeling decision, not a cleanup
Reaching the M4 acceptance (`A△B` over soft-airside ≤60 m == 0) requires picking
one junction model:

- **Make the test match the model (A):** remove check_grade's per-axis junction
  diagonal-skip so it regulates cross-axis junction diagonals at body_cap. This
  makes the grade TEST *stricter* on junctions (flags thousands of currently-
  ignored cross-axis pairs) — the opposite of M4's stated "the test stops
  over-checking, so the 956 unenforced collapse." It would likely *raise* the
  junction violation count and change the M6 baseline.
- **Make the model match the test (B):** give the SOLVER per-axis junction
  grading with the cross-axis diagonal skip. This changes the **build output**
  (solver behaviour) → that is M6 (solver correctness), explicitly NOT autonomous.

Either direction is a substantive, judgment-bearing change to what "a junction
grade violation" means. The per-axis junction rule (longitudinal cL=0.03 /
transverse cT=0.02, cross-axis unregulated) has a documented FAA basis; silently
deleting it from the validator to chase byte-agreement would be guessing.

## What IS safe and was done
- `tools/diff_constraint_graphs.py <ICAO>` — reusable; prints |A|,|B|,∩,∖ overall,
  per role, by distance band, and the ≤60 m acceptance metric.
- The long-apron-chord half (B∖A, >60 m) IS a clean candidate to narrow in
  check_grade (apply `_APRON_BODY_CHORD_MAX_M`) — but it doesn't move the ≤60 m
  acceptance, so it is left for the same human pass that decides the junction model.

## Recommended decision for the human
Decide the junction model first (per-axis-skip vs uniform body_cap), apply it to
BOTH generators (lifting the shared pair-selection into one module both import, as
the plan describes), then re-run `tools/diff_constraint_graphs.py` per fixture to
confirm `A△B ≤60m == 0`, and re-baseline `test_pavement_grade` once with the new
agreed counts. The build OSM is unaffected by check_grade edits (M4 touches only
`tools/`), so byte-identity is preserved regardless; only the test semantics move.
