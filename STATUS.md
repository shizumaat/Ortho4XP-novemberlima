# Auto-Patch Status — taxi-centerline grade compliance (handover)

## TL;DR

This session's through-line: **make taxi pavement follow terrain (DEM) while
keeping every taxi route within FAA/EASA grade**, and chase the last grade
violations toward GREEN baselines.

**THE GOAL (still open):** *along-taxi-centerline grade ≤ the code-letter limit
EVERYWHERE* (longitudinal 1.5% code C-F / 3% A-B; transverse 1.5% / 2%), while
pavement otherwise hugs the DEM. We are NOT there yet — see "The remaining
challenge."

**Everything is committed.** HEAD = `052370d`. Working tree clean.
This session's commits (interleaved with the user's parallel boundary/clearance work):
- `7218926` Elevation: **persistent DEM attraction** — taxiways/aprons follow terrain.
- `c6146d0` Elevation: **taxi→runway anchor + hybrid centerline junction grade**.
- `e88f605` Elevation: **asymmetric DEM floor** — undo spurious below-terrain drag.
(Plus the prior `956fe4f` boundary-interior invariant — VERIFIED in X-Plane: tile
+30+031 went 2.6M→**930k triangles, 9m40s→30s load**.)

**Suite: 9 pre-existing failures** (see `memory/suite_baseline_dev_head.md`):
3× compare_target (SPJC, SPLP×2 — fixture drift, READY TO RECUT), 3× SPJC junction
invariants, 3× grade (SPJC/SPLP/CYXY). This session **improved every grade test's
worst case** but did not zero them.

---

## THE ACTIVE ELEVATION SOLVER (read this first)

`src/auto_patch/elevation_per_surface/unified_jacobi.py::solve` is the LIVE solver
(`USE_PER_SURFACE_SOLVER=1`). The fallback `elevation._solve_pavement_elevations_unified`
is dead — don't edit it. Per iteration it does:

1. **Asymmetric DEM attraction** (this session): pull each SOFT node toward its DEM —
   STRONGLY up if below terrain (`DEM_FLOOR_ATTRACTION=0.85`), gently down
   (`DEM_ATTRACTION=0.3`). Persistent (no decay).
2. **Cap projection** (5 sweeps): enforce per-edge grade ≤ cap. Runs AFTER attraction,
   so **grade wins** (a node that must sit below DEM to grade toward a HARD anchor is
   pushed back down).
3. **Equality groups**: terminal flatness + rect axis-end (cross-section) flatness.

HARD anchors = CIFP runway corners + tile-seam DEM vertices only. Everything else is SOFT.

Junction edges currently = **ring + all-pair Euclidean** (with a hybrid centerline-arc
budget for along-centerline pairs, `c6146d0`). Rects = ring-only + cross-section flatness.

The grade AUDIT (the test gate) is `tools/check_grade.py` — all-pair Euclidean within-shape
+ a triangle plane-gradient check + cross-shape proximity + mid-edge steps.

---

## WHAT THIS SESSION FIXED (committed, verified)

- **HECA stub T4 cliff: 7.25 m → ~1 m.** Root: the solver hard-anchored only runways and
  relaxed everything else by cap-projection-ONLY, so the taxiway/apron network — strung
  between HECA's genuinely-low south terminals (~60 m) and high runways (110 m) — sank 5-8 m
  below its own terrain via cap-chains. Fixes: persistent DEM attraction (`7218926`) +
  taxi→runway anchor reviving `TAXI_ANCHOR_DIST_M` (`c6146d0`) + asymmetric floor (`e88f605`).
  T4 runway-side 103.3→**109.5 m** (DEM 109.3).
- **SPLP stub A: 3.88% → cleared.** The asymmetric floor holds its corners at the FLAT
  ~72 m terrain (they were dragged 1.2 m below by a descending taxiway). SPLP within-shape
  6 → **2**.
- Grade worst-cases all improved: SPJC cross 1.30→0.20 m; SPLP 6.31→ (stub A cleared);
  CYXY worst step 5.15→2.85 m.

---

## THE REMAINING CHALLENGE — SPLP −10025 / −10026 (the GOAL blocker)

Two SPLP junction violations remain: −10025 (**2.87%**) and −10026 (1.98%). These are
**GENUINE along-route terrain transitions**, NOT solver artifacts and NOT cross-axis
diagonals: the taxi route descends from the flat ~72 m apron region to a taxiway that
descends to ~64 m, over ~59 m = **3.04% measured ALONG the centerline**.

**The core tension:** the DEM-floor holds the upstream (flat) pavement AT terrain; to make
the descent ≤1.5% the upstream pavement must dip BELOW terrain to start descending earlier
("spread the descent"). But these are **opposing GLOBAL forces** — and cap-projection's
fixed point is the low-dragged equilibrium regardless of start, so any *global* spread pass
re-sinks the WHOLE field (undoes the floor). There is no global knob that says "spread THIS
one route below terrain but hold everything else at terrain."

### Everything tried for SPLP (all in `memory/splp_stub_a_grade.md`)
1. **Runway-flex** (drop the runway at the threshold to ease the junction): RULED OUT — the
   SPLP runway near here is already at ~1.9% (no grade headroom; boxed by CIFP/seam anchors).
2. **Per-axis junction grading** (along-centerline edges only + code-letter caps; exempt the
   inter-centerline diagonal): architecturally CORRECT and matches FAA/EASA, but does NOT
   clear −10025 (its violation is along-route 3.04%, not the diagonal) and the per-axis solver
   made it WORSE (2.87→3.04%, follows terrain more). REVERTED.
3. **Weaken junction DEM-attraction**: REGRESSED stub A to 5.34%. Wrong direction. REVERTED.
4. **Asymmetric DEM floor**: BEST result (stub A cleared, SPLP 6→2). KEPT (`e88f605`).
5. **Two-phase global spread** (floor phase then pure-cap phase): re-sinks the field
   (stub A 2.43→6.31%). Confirms floor-vs-spread can't be separated globally. REVERTED.

---

## THE GOAL & RECOMMENDED PATH (for the next agent)

**Goal: along-taxi-centerline grade within compliance everywhere, terrain-following elsewhere.**

The clean way to honour BOTH (terrain-following AND a localised, gradual descent on a route
that must reach a low connection) is a **per-route TAXI-CENTERLINE PROFILE pass** — analogous
to the runway FAA-profile redistribution (`runway_redistribute.py` / `runway_regrade.py`),
but for taxi routes:
  - Walk each apt.dat taxi centerline as a 1-D chain between its anchored ends.
  - Fit a grade-compliant longitudinal profile (≤ code-letter cap) that stays as close to
    DEM as the caps allow — letting it dip below terrain ONLY along that route where a
    descent to a low connection requires it.
  - Write those profile elevations as soft targets the per-surface solver respects, so the
    descent is localised to the route instead of diffusing across the field.
  This is a substantial new feature, not a solver-tuning tweak.

**Per-axis + A/B code-letter grading** is a SEPARATE, architecturally-correct feature (fixes
the cross-junction case — a cross-junction tilted 1.5% each way reads 2.12% on the diagonal,
which all-pair wrongly forbids; and enables A/B taxiways' 3%/2% caps). It does NOT clear SPLP.
Helpers already exist: `apt_dat_reader.taxi_size_letters()` → `layout.apt_taxi_letters`
(name→letter from apt.dat row-1202 `TaxiEdge.kind`); config grade-by-letter helpers were
prototyped then reverted with the solver change — re-add when pursuing this. The audit
(`check_grade`) would also need a per-axis junction mode (within-shape + plane-gradient),
threading centerlines (pass them as lat/lon so frames match — check_grade is mean-centred).

**Pragmatic GREEN now (if profiles are deferred):** recut the SPLP grade baseline at the
floor state (2 violations) and document −10025/−10026 as accepted genuine terrain transitions
via a per-junction allowance in `tests/test_pavement_grade.py` (like the existing per-airport
`MID_EDGE_CAP`). Real taxiways do exceed 1.5% at some intersections on sloped terrain.

---

## FAA/EASA grade basis (confirmed this session)

Taxiway grade is regulated PER-AXIS, not "max slope in any direction":
- LONGITUDINAL (along the centre line): ≤1.5% code C-F, ≤3% code A-B (ICAO Annex 14 §3.9 /
  EASA CS-ADR-DSN.D.265).
- TRANSVERSE (across): ≤1.5% C-F, ≤2% A-B (EASA CS-ADR-DSN.D.280).
- Junctions/intersections: geometric guidance only (fillets, sight distance) — NO special
  diagonal-slope rule. The inter-centerline diagonal is an UNREGULATED direction.
So the all-pair Euclidean junction cap is stricter than the regulations require; per-axis is
the correct model.

---

## HOW TO TEST / KEY FILES / GOTCHAS

- Build one airport: `from auto_patch.pipeline import build_airport_pavement;
  layout = build_airport_pavement("SPLP", xplane_root(), compute_elevations=True)`
  (sys.path += `src/`, repo root, `tests/`; `from conftest import xplane_root`).
  Grade check: `import check_grade; check_grade.run_checks(Path(osm), max_grade_pct=1.5,
  proximity_m=1.0, edge_search_m=5.0, edge_step_m=0.5)`.
- Full suite: `venv/bin/python -m pytest tests/ -q` (~2-3.5 min). Baseline = 9 failures.
- Key files: `elevation_per_surface/unified_jacobi.py` (active solver),
  `tools/check_grade.py` (grade audit / test gate), `runway_redistribute.py` +
  `runway_regrade.py` (runway profiles — the model for a taxi-route profile),
  `apt_dat_reader.py` (`taxi_size_letters`, `taxi_centerlines`), `config.py` (grade caps).
- Diagnostics this session left in `/tmp` (regenerate as needed): they build SPLP/HECA and
  dump per-vertex solved-vs-DEM, junction neighbours, along-centerline grade.
- **GOTCHAS:** Ortho4XP GUI caches `auto_patch.*` imports — restart it after editing source.
  The user EDITS FILES IN PARALLEL — re-check `git status`/`git log` before committing and
  commit ONLY your own files. `check_grade` uses a mean-centred meter frame (translation vs
  the layout's anchor frame) — pass centerlines as lat/lon if threading them in.

## Memory pointers (read these)
- `splp_stub_a_grade.md` — full SPLP analysis + every lever tried (THE key handover note).
- `done_dem_attraction_solver.md` — the DEM-attraction fix + active-solver facts.
- `done_boundary_interior_invariant.md` — the X-Plane load-time fix.
- `suite_baseline_dev_head.md` — the 9-failure baseline.
