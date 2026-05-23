# Auto-Patch Status — solver priority cascade + per-axis junction grading (handover)

## TL;DR

This session **rebuilt the elevation solver as a priority cascade** and added
**per-axis junction grading** + a **fast closest-to-DEM spread fit**, all to
clear the last SPLP taxi-grade violations (junctions −10025 / −10026). With the
new machinery enabled, **−10025 and −10026 clear**. It is committed **GATED OFF**
(two flags, both default `False`) because one holdout remains — `stub/A` — and
enabling it today is a lateral move (trades −10025/−10026 for stub/A). The path
to finish is well-scoped below.

**HEAD = `68bed11`. Working tree clean.** This session's commits (interleaved
with the user's parallel `clearance.py` work `474af1d` + `8087ff1`):
- `a4e0bed` Elevation: **priority cascade** (seam>runway>taxi>apron>terminal).
- `2585844` Elevation: **per-axis junction grading** (solver + audit), gated off.
- `e2cc12d` Elevation: **fast spread fit** (over-relaxed cap-projection to
  compliance, replaces the too-slow Dykstra), gated off.
- `68bed11` Test: **rects slope only along axis** invariant (catches a real bug).

**Suite: now 10 failures** (was 9). The +1 is the NEW invariant test
(`68bed11`) catching a genuine malformed rect — see "New invariant" below. The
elevation cascade itself is **non-regressing** (verified 9/324 with flags off,
twice).

Full design notes + every dead-end are in `memory/project_solver_priority_cascade.md`.

---

## THE NEW SOLVER (read this first)

`elevation_per_surface/unified_jacobi.py::solve` is now an **ordered priority
cascade** (user directive 2026-05-22: *"grade is sacred; the cascade order
decides who yields to preserve it"*):

```
seam / runway corners  (HARD anchors, immutable)
  → TAXI network (rects + junctions)   solved against the frozen anchors
  → APRONS                              solved against the frozen taxi network
  → TERMINALS (flat)                    solved against the frozen aprons
```

Each tier is solved with all higher tiers FROZEN as hard anchors. A node's
OWNER tier = the highest-priority role using it (`_TIER_TAXI` > `_TIER_APRON`
> `_TIER_TERMINAL`); a node shared by a taxiway and an apron is taxi-owned, so
the apron yields. Lower tiers couple to frozen higher tiers **through the shared
HARD node, not via cross-tier edges** — so each phase builds only its own tier's
edges (`_build_edges(roles=…, add_runway_anchor=…)`). Helpers: `_node_tiers`,
`_run_phase`, `_TIER_*`, `_TAXI_TIER_ROLES`.

**Why phased, not simultaneous:** the old single Jacobi was a tug-of-war — an
apron held at DEM by its own attraction pinned a taxiway it bordered, forcing
the taxiway over-grade. Freezing the higher tier removes that conflict.

### Two flags gate the new behaviour (both default False = non-regressing)
- `_USE_L2_FIT` — taxi/apron phases use `_compliant_spread_fit` (below) instead
  of the proven DEM-attraction relaxation (`_run_jacobi`, asymmetric floor).
- `_PER_AXIS_JUNCTIONS` — junction edges = ring + along-centerline only; the
  unregulated inter-centerline DIAGONAL is dropped. PAIRS WITH the audit (see
  below) — flip them together.

### The spread fit (`_compliant_spread_fit`, commit e2cc12d)
The DEM is a **preference, not a constraint** (user). Soft nodes seed at terrain,
then every over-grade edge is cap-projected and the excess PROPAGATES into the
network until nothing exceeds grade (the descent dips below / rises above terrain
and smooths out). Stops on **max VIOLATION < tol** (true compliance), not per-iter
change. `_SPREAD_OMEGA = 1.0` — **SOR ω>1 DIVERGES** here (tested 1.5→5.9%,
1.9→23%). Converges in ~100 iters on a FEASIBLE region; on an INFEASIBLE span
(bounding hard anchors >1.5% apart in elevation) it stalls at the min achievable
grade — that is correct behaviour, the region genuinely has no compliant profile.
(The earlier Dykstra `_l2_compliant_fit` was L2-exact but never finished
propagating — 400k iters == 40k. Replaced.)

### Per-axis AUDIT (`tools/check_grade.py`, commit 2585844)
`run_checks(..., taxi_axes_ll=…)` grades JUNCTION within-shape pairs per-axis
(`_per_axis_allowance` + `_project_to_polyline`, pure-math): a pair is graded only
if both ends are within 15 m of a COMMON centerline (allowance = cL·long + cT·trans);
cross-axis diagonals are skipped (unregulated). **CRITICAL:** the centerlines MUST
come from `layout.apt_taxi_centerlines` (apt.dat — what the build used), passed as
lat/lon. **Never re-derive from the OSM** (the patch OSM has no centerlines, only
`aeroway=taxiway` polygon footprints; raw OSM diverges from apt.dat). The grade
test (`tests/test_pavement_grade.py`) builds `taxi_axes_ll` ONLY when
`unified_jacobi._PER_AXIS_JUNCTIONS` is True, so solver + audit flip together.
Default off → audit unchanged.

---

## VALIDATED RESULT (flags on)

`_PER_AXIS_JUNCTIONS=True` + `_USE_L2_FIT=True` on SPLP → **−10025 and −10026
CLEAR**, cross-shape 0, no new diagonal violations. The architecture works.
Net trade today: clears −10025/−10026 (2.87% / 1.98%) but `stub/A` then reads
2.43% — same violation COUNT, plus slower — so **not yet worth enabling by
default** until stub/A is closed.

---

## THE HOLDOUT — stub/A (the next focus)

Two SPLP locations carry the name; keep them straight:
- **Apron-side stub/A, local ≈ (60, 471)** = ll `-12.1560,-76.9982`. THIS is the
  one the user inspected. Under flags-on it reads 2.43% (0.5 m over 20.6 m).
  Its **OSM data is actually clean**: coplanar (plane-dev 0.02 m) and slopes
  ALONG its centerline (slope-dir · source_axis = 1.00; source_axis (0.91,−0.42)
  matches the apt.dat A-leg). So the X-Plane "perpendicular slope" the user saw
  is **not reproducible from the data** — likely the near-square irregular quad
  (sides 21.6/21.7/20.6/28.6) or a rendering nuance; UNRECONCILED, worth a look.
  It bridges two junctions sitting 0.5 m apart (HIGH-side junction ll
  `-12.1559931,-76.9977909`, area 3729, toward the runway ~71–73; LOW-side
  junction ll `-12.1555924,-76.9987070`, area 7489, the −10025 descent down to
  68.7). A 20.6 m stub can't span 0.5 m at ≤1.5%.
- It is **NOT runway-pinned directly** (55 m from the runway, > the 30 m taxi-
  anchor distance) — pinned via the JUNCTION chain. At 200k spread-fit iters it
  is identical to 40k ⇒ **genuinely infeasible as currently anchored**, not slow.

**User's key insight for the fix (2026-05-22):** in X-Plane the LOW-side junction
"appears nearly flat between the B taxiway and stub/A — it could easily slope up
more toward the runway to ease the climb." That is the lever: if the low junction
graded UP toward the runway (deviating ABOVE terrain — allowed, DEM is a
preference), stub/A's low end rises and the stub eases to ≤1.5%. The spread fit
keeps it near-DEM (flat there) instead of using available grade to climb. Two
ways to realise it:
  1. **Mid-runway connection flex** (task #9): let the runway corner that is a
     taxi connection (not a threshold) flex within runway grade so the junction
     can grade away from it. Limited headroom (runway ~1.7–1.9% there).
  2. **Bias the junction grade toward easing connected stubs** (climb toward the
     higher anchor rather than sit at DEM) — closer to what the user described.
Also flagged by the user: the LOW-side junction joins taxiway **B with only one
node** — possibly a separate connectivity bug worth checking.

If neither pans out: accept stub/A's ~2.4% as a genuine threshold transition and
recut the SPLP grade baseline (the main targets −10025/−10026 are cleared).

---

## NEW INVARIANT (commit 68bed11) — caught a real bug

`test_sloping_rect_slopes_only_along_axis` (in `tests/test_pavement_geometry.py`):
a taxi rect may slope only along `source_axis`, so its two axis-end edges
(perpendicular to the centerline) must be FLAT. Catches **1 genuine malformed
rect at SPLP: ref A @ local (−123, 98)** — a degenerate ~2 m-wide sliver
(sides 2.0/28.1/89.8/92.2) with altitudes [65.2, 64.2, 64.2, 61.9] → a 3.30 m
axis-end delta (47 m plane-dev). This is a **geometry bug at the source** (a
clip/seam pass emitting a degenerate sliver), NOT an elevation-solver issue —
fix the rect builder / clip so it never emits a 2 m sliver. (This is a DIFFERENT
shape from the apron-side stub/A the user inspected.) Baseline is now 10 failures
because of this deliberate new catch.

---

## HOW TO TEST / KEY FILES / GOTCHAS

- Build one airport: `from auto_patch.pipeline import build_airport_pavement;
  layout = build_airport_pavement("SPLP", xplane_root(), compute_elevations=True)`
  (sys.path += `src/`, repo root, `tests/`; `from conftest import xplane_root`).
- Flip the flags for an experiment (do NOT commit them on):
  `from auto_patch.elevation_per_surface import unified_jacobi as uj;
   uj._PER_AXIS_JUNCTIONS = True; uj._USE_L2_FIT = True` BEFORE building. To audit
  per-axis, pass `taxi_axes_ll` to `check_grade.run_checks` built from
  `layout.apt_taxi_centerlines` + `apt_taxi_letters` (see test_pavement_grade.py).
- Full suite: `venv/bin/python -m pytest tests/ -q` (~3–5 min). Baseline = **10
  failures** (the 9 prior + the new sloping-rect invariant).
- Diagnostics left in `tools/` (regenerate freely): `diag_splp_centerlines.py`,
  `diag_splp_junction.py`, `diag_splp_corridor.py` (1-D profile prototype),
  `diag_splp_stubA.py`, `diag_splp_runway.py`, `diag_coplanar.py` (rect plane-dev
  scan), `verify_splp_grade.py` (build + check_grade). `/tmp/exp*.py`,
  `/tmp/diag_*.py` were scratch (gone on reboot; reconstruct from the tools).
- **GOTCHAS:** the user EDITS FILES IN PARALLEL — re-check `git status`/`git log`
  before committing and commit ONLY your own files (this session: only
  `unified_jacobi.py`, `check_grade.py`, the two test files; `clearance.py` +
  `config.py` are the user's). The runway DOES cross the tile seam at 02/20's
  south end (lon −77.0, ~56 m) — but −10025 is ~1 km NORTH of it (mid-runway
  region ~71 m), so the seam is not what binds −10025/stub/A.

## Memory pointers (read these)
- `project_solver_priority_cascade.md` — the full cascade + per-axis + spread-fit
  design, every dead-end (soft-targets, Dykstra, plain-cap, SOR), and the stub/A
  diagnosis. THE key handover note this session.
- `splp_stub_a_grade.md` — prior SPLP analysis (pre-cascade; some superseded).
- `suite_baseline_dev_head.md` — the pre-existing 9-failure set (now +1 = 10).
