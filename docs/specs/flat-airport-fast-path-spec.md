# Flat-airport fast path — specification

Status: approved for implementation 2026-07-17 (owner directive).
Extends the flatness-certified lazy tier of 2026-07-05
(`O4_FLAT_SHAPE_LAZY`, `solver_primitives._certify_flat_shape`) and the
recorded next-tier plan in the 2026-07-04 performance round.

## 1. Motivation

OTHH (Hamad International, dead-flat coastal fill) took **660 s** of
auto-patch wall time on its first build (2026-07-17); the owner's
expectation is that even the largest airport stays **under ~10 minutes,
with flat ones far under**. The 2026-07-04 KDFW profile located the
costs: within-shape pair construction (`_build_shape_constraints` /
`grade_law.classify_pair`, ~108 s instrumented, 2.5 M pairs), clearance
emission (~144 s), `final_grade_projection`'s full law-graph rebuild
(~89 s), reach-band construction (`building_feasibility`, minutes at
airports with many buildings), plus terrain-independent geometry/emit
(~210 s real — explicitly OUT of scope here, it has its own queue).

The insight the owner stated and this spec operationalizes: **on flat
terrain, huge swaths of nodes — often all of them — are already
feasible and already in grade no matter what the solver does.** Work
that exists only to find or repair grade violations is provably
unnecessary wherever a conservative certificate shows the DEM already
satisfies every law that could apply. This must accelerate every
airport, not just wholly flat ones: any sufficiently flat REGION of any
airport skips the same work.

## 2. Standing constraints (rulings that bind this design)

1. **Verification is untouched.** Runtime validators stay pure
   reporters; `tools/check_grade.py` still measures every emitted
   patch. A certificate skips *construction and solving*, never
   *checking*.
2. **Certificates sample the airport-smoothed DEM** through the exact
   sampler the node seeds use (`elevation._sample_dem`, layout anchor
   frame) — bit-identical values, per the seam ruling. Any sampling
   gap refuses the certificate (fail toward correctness).
3. **Hard classes are never certified**: runway / seam / join nodes sit
   at profile (birth-datum) values, not DEM seeds. Crossing-terrain
   zones, bridges, tunnels, object pads, and portal crowns override
   terrain by design and are excluded from every certificate region.
4. **Buildings are FLAT** (owner ruling): a building seat certifies
   only against the seat tolerance, never the apron grade budget.
5. **Margin accounting is explicit.** A certificate consumes at most
   `FLATNESS_CERTIFICATE_RATE_FACTOR` (existing 0.6) of the tightest
   applicable budget, and the remaining slack funds the movement
   tolerance (`lazy_move_tolerance`) so harmonic smoothing cannot void
   it — the 2026-07-05 lesson ("certificates all expanded") must never
   regress. Constants live in `config.py`, one copy.
6. **Determinism**: every new iteration order is `sorted()`
   (nondeterminism ruling); no wall-clock or hash-order dependence.
7. **No silent caps**: every gate logs one per-airport summary line —
   shapes certified / expanded / refused, stages skipped — so a
   mis-firing gate is visible in the console and in replays.
8. Profiling first: every work package re-profiles
   (`tools/profile_airport_build.py`) before and after; cProfile
   inflation is per-call (2026-07-04 gotcha) — claims of real seconds
   saved come from wall-clock phase timings, not instrumented totals.

## 3. Design

### 3.1 Tier 0 (exists — baseline to build on)

Per-shape lazy pair generation for apron/junction shapes
(`O4_FLAT_SHAPE_LAZY`, default on): certified shapes carry
`lazy_expand` / `lazy_seed` / `lazy_move_tolerance`;
`one_solve.feasibility_project` expands an entry only when a node moves
beyond the slack-aware tolerance. Scoped final projection
(`O4_SCOPED_FINAL_PROJECTION`) defers proven-unchanged shapes.

### 3.2 Tier 1 — certificate coverage (`O4_FLAT_CERTIFICATE_COVERAGE`)

Extend certification to the shape classes that today always build
eagerly:

- **Taxi rects**: axial edges budget `TAXIWAY_MAX_GRADE · dist`,
  flat-cross edges budget ≈ 0 — a rect certifies when its axial DEM
  relief rate is ≤ `rate_factor · TAXIWAY_MAX_GRADE` AND its cross
  relief is within the flat-cross tolerance plus smoothing reserve.
- **Terminals / building seats**: certify against the seat flatness
  tolerance (the value the terminal-flat law enforces), not a grade
  rate. A certified seat skips its **reach-band construction** in
  `building_feasibility` (the per-building band Dijkstras exist to
  find a feasible seat; a seat whose whole footprint DEM relief fits
  the tolerance is feasible at its DEM mean by inspection — record
  that value as the band result through the same result structure).
- **Boundary / groundside shapes** whose role has a grade limit:
  same pattern, their own budgets.

Every extension reuses `_certify_flat_shape`'s sampling and refusal
discipline; per-class rate constants go in `config.py` next to
`ROLE_GRADE_LIMITS`.

### 3.3 Tier 2 — whole-airport fast path (`O4_FLAT_AIRPORT_FAST_PATH`)

Computed AFTER phase-1 geometry, BEFORE the solve stages, in
`route_profile/solve.solve_route_profile`:

A `FlatAirportCertificate` holds when:
- every soft shape certifies under Tier 0/1, AND
- every runway's along-axis DEM relief satisfies the runway profile
  budgets (`RUNWAY_END_GRADE` at the end zones, `MAX_RUNWAY_GRADE`
  elsewhere) at `rate_factor` margin, AND
- no bridge / tunnel / crossing-terrain / object-pad subsystem claimed
  any geometry at this airport (their presence = not a flat airport in
  the sense that matters).

When it holds, the solver stages collapse:
- runway profiles still run (birth-datum law; on flat input they
  converge immediately and keep the single-source law intact),
- **reach bands, spine profile, body fill, and feasibility iteration
  are skipped entirely**; every soft node takes its DEM seed value,
- write-back, emission, decimation, and verification run unchanged
  (transition features whose magnitude is ~0 already collapse in the
  existing emit paths),
- the final grade projection runs in scoped mode with every certified
  shape deferred (Tier 0 machinery) — on a fully certified airport it
  should visit approximately nothing.

If ANY certificate refuses, the airport takes the normal path; the
fast path is an optimization with a provable precondition, never a
behavioral mode.

### 3.4 Tier 3 — solve-stage narrowing at mixed airports (LAND LAST)

At airports where only part of the field certifies, narrow the
expensive stages to the uncertified remainder (spine/body-fill node
sets, projection scope). This touches `final_grade_projection` and the
late-projection pass — the SAME code the drive-to-zero session is
actively changing — so Tier 3 waits until that work settles and is
briefed as a separate, coordinated package. Tiers 1–2 must not touch
those files beyond the existing defer-list parameter.

## 3.5 Program performance target (owner ruling 2026-07-17)

**≤ 60 seconds wall for the most complex airports (OTHH/KDFW class),
≤ 10 seconds for typical airports** (cold airport, warm tile caches;
per-airport wall — tile wall is already the parallel maximum).
"Seconds for the most complex airports" is explicitly OUT of scope:
that requires a compiled core, a different program.

The budget breakdown this implies at the OTHH class:
- terrain-dependent construction + solve (887 s baseline, ~700 s of
  band machinery): reduced to ≤ 20 s by Tiers 1–3 plus the research
  program (raster masked Dijkstra fields, well-separated-pair
  certificates, chromatic Gauss–Seidel, closed-form chains — see
  docs/research/ surveys on the flat-fast-path-tier3 branch);
- terrain-INDEPENDENT geometry + emission (~200 s at KDFW): reduced
  to ≤ 40 s by Wave 3 below — previously "out of scope", promoted by
  this ruling because the 60 s target is unreachable without it.

### Wave 1 outcome (2026-07-18) — premise refuted, requirements updated

Wave 1 REFUTED this spec's assumption that reach bands are consumed as
pure clamp inequalities. The measured consumer map: every
``node_band`` consumer clamps an ITERATIVELY-MOVED elevation (each
Gauss–Seidel sweep, spine solves, curvature fairing) and two consumers
READ band values (spine floors from band ceilings; seat construction).
Consequence, proven empirically at HECA (conservative Lipschitz-widened
intervals: mean |Δelev| 3.5 m, max 83 m, within-shape violations
1895→1912 — WORSE): **conservative band intervals shift the solver
fixpoint and are PROHIBITED for ``node_band``.** Wave 2's raster field
must therefore produce EXACT per-node envelope values (grid-metric
exactness; resolution fine enough that grid-vs-continuous distance
error stays small), with counts-not-worse acceptance — and must keep
the solve's band producer and the validator's
(``grade_graph_validate.route_band_violations``) semantically aligned.
The wave-1 serving-line amortization shipped byte-identical but
performance-neutral and is gated OFF by default (kept as scaffolding).

### Retrospective 2026-07-18 — program state moved to the track board

A four-audit retrospective (post waves 2c+3) found this section's
arithmetic stale: the "~700 s of band machinery" pool was already
collapsed by the wave-2 raster field (band cost now ~33 s at OTHH),
Tier 2's refusal set means the whole-airport fast path structurally
never fires at the OTHH class (§4.4 acceptance unmeetable as written
— owner ruling pending), and ~100 s of current OTHH cost postdates
this spec (double final projection, adjacent-ground presolve,
stitching, gap fill, chromatic coloring overhead). **The live plan,
measured cost map, and track statuses now live in
``docs/build_time_program_board.md``** — update that board, not this
section. This spec remains authoritative for the tier/certificate
DESIGN and the standing constraints in §2.

### Enforcement (owner ruling 2026-07-18 — hard law, all sessions)

Canonical text: repo-root CLAUDE.md, working-style item 6 (two budgets
— 60 s per-airport auto-patch, 300 s whole-tile compute, both cold and
excluding downloads; >=1 % review trigger; budget-crossing needs owner
approval). This spec section is a POINTER, not a second copy.

### Wave 3 — geometry & emission acceleration (terrain independent)

Scope: the phase-1 shape construction and emission paths (rect
building, junction partitioning, clearance cuts, boundary ribbon,
decimation, OSM writing). Levers, in expected order of value:
vectorized shapely 2 batch predicates (`contains_xy`,
`intersects` on coordinate arrays), prepared geometries and STRtree
bulk queries replacing per-shape Python loops, single-pass
union/difference restructuring where profiles show repeated
re-unioning, and emit-side numpy vectorization. Acceptance:
byte-identical output (geometry is deterministic — there is no
tolerance story here), fixture suite green, per-phase profile
before/after. Wave 3 starts after Tier 3 wave 2 lands and is briefed
from a fresh profile — geometry costs will have shifted by then.

## 4. Acceptance

1. **Inertness proof**: on a hilly fixture airport where no
   certificate fires (MMOX or EGPB replay), gate on vs off outputs are
   byte-identical (same-path stash A/B discipline does not apply —
   use env-gate A/B in one working tree).
2. **Fixture integrity**: full `tests/` suite green; `check_grade`
   violation counts not-worse on SPJC / SPLP / CYXY / HECA fixtures
   (the counts-gate precedent from the worklist Gauss-Seidel round).
3. **Performance targets** (wall clock, warm caches,
   `tools/profile_airport_build.py`):
   - OTHH: 660 s → **≤ 180 s** auto-patch wall.
   - KDFW: gated phases (shape constraints + reach bands +
     feasibility) ≥ 3× faster; whole-airport wall reported.
   - CYXY (small, hilly): within noise of baseline (gate must cost
     ~nothing when it does not fire).
4. **Counters**: the per-airport summary line reports certificate hit
   rates; OTHH must report (approximately) all shapes certified and
   the fast path taken.
5. Every new module: type hints, docstrings, no `exec`/`eval`, no GUI
   imports, tests headless under `tmp_path` (repo conventions).

## 5. Work packages

- **WP1 (Tier 1 + instrumentation)**: extend certification coverage
  (rects, seats, groundside), reach-band skip for certified seats,
  per-airport counter line, and a fresh OTHH + KDFW phase profile
  before/after. Files: `elevation_per_surface/solver_primitives.py`
  (additive), `elevation_per_surface/building_feasibility.py`
  (additive), `config.py` (constants), new tests.
- **WP2 (Tier 2)**: `FlatAirportCertificate` + the solve-stage
  collapse in `route_profile/solve.py` (single early branch), the
  fixture A/B inertness harness, and the OTHH acceptance run.
- **WP3 (Tier 3)**: deferred; scoped after the drive-to-zero
  projection work lands.

Out of scope (recorded): mesh/Triangle4XP costs, DEM inset fetch time
(separate cold-tile issue), per-tile OSM sub-extracts, and any
compiled-core/GPU rewrite ("seconds at OTHH" is explicitly not a
target). Geometry/emission acceleration was PROMOTED from this list
to Wave 3 by the 2026-07-17 ≤60 s ruling (§3.5).
