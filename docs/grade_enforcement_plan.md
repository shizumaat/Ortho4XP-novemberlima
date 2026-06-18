# Plan: guaranteed grade compliance — zero within-shape / route violations

Status: PROPOSED (2026-06-18). Goal set by user: the solver must **truly
enforce grade** along taxi routes, buildings, and aprons — **absolutely no
violations, not "close enough."** Companion to `docs/pipeline_geometry_audit.md`
(the geometry-sequence audit) and `docs/elevation_solver.md` (the solver model).

## Diagnosis — residuals are tolerated infeasibility, not weak enforcement
The active solver (`elevation_per_surface/unified_jacobi.py:solve()`) already:
- builds a constraint graph matching the validator's geodesic-visibility pairs
  for aprons/junctions (`_build_edges`, `_visible_grade_edges`),
- computes proper **2-sided difference-constraint bands** `[lo,hi]` by Dijkstra
  from the hard anchors (`_grade_bands`),
- clamps nodes into those bands hard (`_project_within_bands`).

Where the system is **feasible it already reaches zero**. Every residual traces
to infeasibility the solver *detects and accepts* instead of *resolving*:
1. **Band-pinned nodes** (`lo > hi`): two hard anchors closer in the graph than
   their elevation gap allows — held at best-effort (`band_pinned`).
2. **Both-endpoints-held over-cap edges**: the projection `continue`s past the
   over-grade edge when both ends are immovable — skipped, not fixed.
3. **Rect flat-end coupling**: a rect is a rigid 2-DOF plane; when its coupled
   group can't satisfy both ends' bands the plane stays over-grade.
4. **Soft↔soft tolerance** (~mm) and **cross-axis junction diagonal** exemptions.

So this is a **difference-constraint feasibility** problem: feasible iff no
anchor pair's elevation gap exceeds the shortest cap-weighted path between them.
The solver computes the bounds; the missing half is **acting when they invert**
and **routing the unavoidable steepness to a legal element** instead of leaving
an in-surface violation.

## Confirmed design rule (user 2026-06-18) — the "steepness sink"
When geometry forces unavoidable steepness (two surveyed truths too close for any
compliant surface): **corridor flex first** — the taxi corridor absorbs the drop
by flexing within its route band; **then an explicit transition element** — a
discrete ramp (≤4%) or a wall/step that carries the drop with no graded surface.
**Apron / taxi / building interiors NEVER hold a violation.**

## Workstreams (staged, each gated, each fixture-verified, byte-identical-off)

### W1 — Solver/validator graph lockstep (constraint level)
Make the solver enforce *exactly* the pair set `tools/check_grade.py` checks —
same visibility gate, same `ROUTE_FIELD_LOCAL_WINDOW_M`, same per-axis rules — so
"solver thinks compliant, validator flags it" is impossible. Add a debug assert
that the two per-shape edge sets are identical. Low risk, foundational.

### W2 — Exact projection to zero tolerance
Drive `_project_within_bands` to a true 0 (per-move re-clamp; tighten
`_SPREAD_COMPLY_TOL_M`) so no soft↔soft pair settles non-compliant. Replace the
silent `continue` on both-held over-cap edges with a **flagged infeasibility**
handed to W3. After W1+W2, every remaining violation is a *true* infeasibility.

### W3 — Feasibility-restoration loop (core)
Replace "tolerate band-pinned" with a resolution cascade applied to each detected
infeasibility, looping until feasible, in the confirmed priority:
1. **Yield the stiffest non-truth anchor** (terminal yields toward its band —
   already the model; make it mandatory before giving up).
2. **Flex the corridor** within its route band (W4).
3. **Split the carrier shape at the grade break** into pieces joined by a
   **transition element** (ramp ≤4%, or wall/step, no in-surface cap) —
   DISCONNECTED nodes (per `elevation_solver.md` rejected-approaches: "pieces
   must be DISCONNECTED at grade breaks — separate nodes + a wall/ramp").
4. Genuine runway-vs-seam truth conflicts get an explicit graded transition,
   never an in-surface violation.

### W4 — Taxi routes as 1-D capped profiles (not 2-DOF planes)
A long rect can't be one plane across terrain (infeasibility #3). Model each route
as a **piecewise-linear longitudinal profile** (grade + vertical-curve capped,
reuse `runway_regrade`/`runway_redistribute` machinery); rects conform to it.
Guarantees along-route compliance by construction and gives the corridor the flex
the hierarchy wants.

### W5 — Geometry feasibility prepass (don't manufacture infeasibility)
Before the solve: do not create thin shapes bridging disparate levels (the
2026-06-18 rect-cap experiment showed a 2 m cap manufacturing 20%+ grades by
tying together individually-compliant vertices of two wide aprons). Split
non-convex aprons/junctions at grade breaks into convex pieces + explicit
transitions. A convex shape's all-pair constraint is always satisfiable from its
boundary, removing a whole residual class.

### W6 — Zero-violation gate
`check_grade` becomes a hard post-solve assertion: any within-shape or route-band
violation FAILS the build (and in the loop triggers W3). Wire SPJC/CYXY/HECA/SPLP
as zero-violation fixtures. "Close enough" stops being representable.

## W1 + W2 RESULTS (2026-06-18 — built & measured)
- **W1 done**: `tools/check_grade.py` now exposes `iter_shape_grade_constraints`
  — the SINGLE generator of constrained pairs, consumed by the validator
  (`_check_within_shape`) and the oracle. Verified byte-identical
  (git-stash diff). Lockstep gap noted: the build's within-shape *reporter*
  over-reports vs the windowed validator (a `d=92 m` SPJC pair) — reconcile.
- **W2 done**: `tools/grade_feasibility_audit.py` — the difference-constraint
  feasibility oracle. Combines BOTH laws (within-shape visibility edges +
  the route-band runway-reach law via `route_field.route_band_violations`),
  computes 2-sided bounds per node, classifies every violation as
  FUNDAMENTAL (band-pinned, `lo>hi` — no compliant field) vs FEASIBLE-but-
  unenforced. **Key lesson: the within-shape law alone does NOT bound deep
  pavement to the runway — the route-band law is essential** (15/25 SPJC
  nodes were unbounded without it).

  | fixture | violations | fundamental (W3/W5) | feasible-unenforced (W2/W4) |
  |---|---|---|---|
  | SPJC | 1   | 0  | 1   |
  | CYXY | 17  | 0  | 17  |
  | HECA | 66  | 31 | 35  |
  | SPLP | 149 | 0  | 149 |

  **Finding that re-weights the plan: 3 of 4 fixtures are 100% feasible-but-
  unenforced** — zero fundamental infeasibility, so the solver could reach
  zero there with W2/W4 alone (NO geometry surgery). **HECA is the exception**:
  31 genuine fundamental infeasibilities (nodes band-pinned by 0.5–0.6 m —
  the terrain canyon; two anchors closer in-graph than their elevation gap).
  HECA is where W3/W5 (corridor flex → transition) is actually required. The
  rect-cap experiment does NOT manufacture fundamental infeasibility (SPJC
  gate-on: all 25 feasible-but-unenforced) — confirming it's a solver-
  enforcement gap, not geometry.

## W2-proper ROOT CAUSE (2026-06-18 — diagnosed, fix is a band reformulation)
Why feasible violations survive (the `O4_W2_DUMP=1` post-projection dump,
`unified_jacobi.py` ~L2286, breaks down each surviving over-cap edge by
held-reason): **the enforce bands FALSELY INVERT.** Under `ROUTE_FIELD_MODEL`
the band (`_runway_reach_bands`, L1785) is anchored on the CURRENT solved
field (`extra_points=field_pts`, the NETWORK_PROFILE_MODEL vertices) + held
writes, then `_lipschitz_tighten_bands` (L1846) squeezes it — a
SELF-REFERENTIAL construction. Where the field is locally imperfect this
manufactures `lo>hi` (CYXY: **528 nodes** with inverted bands, widths down to
−2.5 m) on nodes the oracle PROVES feasible. A band-pinned node is HELD out of
`_project_within_bands` (both-held edges are `continue`d, L773), so its real
grade violation can never be fixed. Secondary: ~209 tiny `(free,free)` edges
left under the 20 mm tolerance / no per-move re-clamp (sub-noise, but must hit
0 for "absolutely zero").

**Naive rescue FAILED (measured, reverted):** widening each falsely-pinned
node to the CLEAN runway/seam route band made CYXY WORSE (26→31 build-reporter
viol). Widening removes the field-tie that building-flatten relies on AND
hands the weak projection more free nodes than it can converge. So the band
inversions are real, but the cure is not "loosen the pinned ones."

**The actual W2-proper fix (next):** stop deriving the HARD band from the
imperfect field. Compute clean, oracle-style FEASIBLE bounds (runway-route +
within-shape tightening, NO field self-anchor) as the hard constraint — these
don't invert on feasible airports (the oracle proved it) — and move the
field-tie / corridor-level preference from a band CONSTRAINT to a projection
OBJECTIVE/attractor. Pair with a CONVERGENT projection (per-move re-clamp to
the bounds, 0 tolerance, no both-held skip on feasible edges). This is the
heart of W2 and overlaps W4 (the field/profile model); it carries
building-flatten / corridor-tie regression risk, so it must be gated and
measured against SPJC building20 + HECA terminal flatten, not blind-landed.
The `O4_W2_DUMP` diagnostic + `tools/grade_feasibility_audit.py` are the
instruments to drive it to zero.

## W2 SOLVER BUILT (2026-06-18, gate `O4_W2_BANDS` default off = byte-identical)
The within-shape enforce was three accreted cap-projection passes, each with its
own artificial constraint (corridor held-write anchors, ±2.5 m movement clamp,
field self-anchor) that EMPTIES the feasible polytope → the projection stalls.
Validated in `tools/grade_feasibility_audit.py`: plain POCS (cyclic edge
projection + box clamp) converges to ZERO on the TRUE feasible polytope
(CYXY 36 sweeps, SPJC 270, SPLP 36) — so the fix is "one clean projection," not
a fancier solver. Under the gate:
1. Band anchored on TRUTH ONLY (runway/seam/base_hard) — drop the field
   self-anchor AND the corridor held-write anchors.
2. Final pair-law closure box = the clean feasible band `[lo,hi]` where finite &
   feasible; bounded ±2.5 m fallback where band-pinned/unbounded (so POCS can't
   diverge at genuine infeasibilities).
3. Terminals held FLAT via their coupling group but LEVEL FREE to yield (not
   pinned at the cascade value) — keeps building20 flat while freeing the
   polytope.

Measured (within-shape validator):
  | airport | baseline | W2 | note |
  |---|---|---|---|
  | CYXY | 17 | **0** | converged (resid 0.001 m) |
  | SPJC | 1  | **1** | the 1 is the post-solve tunnel-ramp feature; airside **0** |
  | SPLP | 149 | **91** | improved + bounded; 4 fundamental remain (W3/W5) |
  | HECA | 66 | **60** | improved + bounded; 305 pinned reps remain (W3/W5) |

So W2 drives the FEASIBLE airports to zero and IMPROVES the infeasible ones
without diverging. building#30 (185k m², the building20 case) stays FLAT;
minor 0.1–0.2 m slope appears on a few SMALL pads (coupling not fully holding
them — not validator violations; a follow-up). Gate-off byte-identical (CYXY
md5 match). NEXT: tune small-pad terminal coupling; then W3/W5 for HECA/SPLP.

## Suggested order
W1 → W2 (foundational, low risk, turn residuals into *true* infeasibility counts)
→ measure how many/where true infeasibilities are per fixture → W5 + W4 (remove
manufactured + planar infeasibility) → W3 (resolve the rest) → W6 (lock it in).

## Relation to the rect end-cap experiment (2026-06-18)
The cap work (gate `RECT_END_CAPS`, default OFF = byte-identical) made taxiway L
grade as a clean full-length plane and improved conformance, but exposed the
tolerated-infeasibility above as steep 2 m caps. It is **superseded by W4/W5**
(routes as profiles; don't manufacture thin shapes) and remains gated-off as a
diagnostic. Keep or drop after W4 lands.
