# Network Profile Model (#4) — implementation design

> ⚠ **SUPERSEDED (2026-06-30 audit).** Built in s78, then folded into
> `route_profile`/`grade_graph`; the standalone `network_profile.py` module is deleted.
> Live model → **`anisotropic_edge_handling_plan.md`**. Carried-forward open directives
> (#198 switchback-as-road decomposition; "no shape may check grade across grass")
> are tracked in **`OPEN_ITEMS.md`**.

**Status: DESIGN, user-approved direction (s77p3, 2026-06-11: "I think
this is the right direction... solve the full centerline taxi network,
which includes curves, solve every intersection, similar to crossing
runways, so they always agree, then we should be able to much more
smoothly map that to the geometry because we'll have a clear profile
running through almost everything").  Not built.  This document is the
handover for the implementing session.**

Written 2026-06-11 at the close of s77 part 3 (dev @6555527).  Read
together with: repo `STATUS.md` (s77 all parts), memory files
`write_arbitration_terminal_leaf.md` and
`apron_geodesic_grade_investigation.md`, and
`docs/route_field_model.md` (#3 — built s76; this model is its logical
completion).

---

## 1. What changes, in one paragraph

Today the airport has TWO disconnected elevation representations: the
**centerline route graph** (the full apt.dat taxi network) which is used
only as a measuring tape — route-band distances, seed bands, flex-demand
depths — and never carries a solved elevation; and the **rect-chain
corridor profiles** which carry elevations but cover only where taxi
rects form chains (multi-rect or runway-touching singletons).  The
network profile model solves **one smooth elevation profile over the
full centerline graph itself** — every surviving apt.dat line, curves
included, apron lanes included — with runway contacts as hard anchors
and every intersection a SHARED VERTEX that agrees by construction (the
crossing-runways rule generalized to the whole network).  All pavement
geometry is then graded FROM that field: rect planes sample it along
their axes, junctions twist between crossing profiles, apron vertices
bind to it through the interior-geodesic value bands, terminals leaf off
their aprons.  The validator derives its bands from the SAME field —
one law, one entry point, no st-band-vs-vertex-band duality.

## 2. Why (the measured evidence — do not re-litigate)

Every major s77 failure class is the two representations disagreeing:

- **Coverage holes** (user, in-sim): taxiway B at HECA = five non-runway
  singleton stubs — dropped by the `_touches_runway` chain gate; the
  B/C lanes crossing apron #198 have no rects at all.  Nothing grades
  them; the surroundings followed raw relief.  The lanes ARE in the
  centerline graph — a graph profile covers them for free.
- **The #193 valley** (user: 30.1131103, 31.4074412): route bands
  measured THROUGH the graph have entry/coverage artifacts; with no
  profile on the network and chords windowed at 80 m, a 9.9 m DEM
  valley stood inside one apron.  (Patched in s77p3 by the two-rate
  geodesic law + pinned placement, but the band field is still derived,
  not solved.)
- **Band self-contradiction** (#256 trace): G's st-band CEILING said
  ~100.9 at the same physical spot where the per-vertex band FLOOR said
  104.17 — two band computations entering the same graph at different
  nodes.  One solved field has one value.
- **The tie machinery exists only to stitch independent chains**:
  crossing inserts, mouth ties, equality groups, damped consensus,
  freeze order, partial ties, blocker rescue, post-freeze reconcile,
  bridge ties, skew end caps — s73-p7 through s77p2, hundreds of lines,
  each fixing one seam class.  On a shared-vertex network the seams
  cannot exist: a crossing IS one variable.
- **Squeeze concentration**: the 05C↔taxiway-A infeasibility (~6-8 m
  over the legal budget) surfaces wherever the last freeze left it —
  28.7 % walls at #256, 29 % on connector D #285.  A network solve
  spreads an irreducible residual along kilometres of route (predicted
  ≤ ~0.4 % over-grade network-wide instead of metre walls at seams).
- **s77p3 singleton experiment (MEASURED, then reverted @6555527)**:
  partially coupling the network (profiling all singletons) closed #256
  fully and restored A5, but pushed standalone >5 % walls 21 → 46 —
  partial coupling MOVES disagreements faster than it resolves them.
  The lesson: ship the network solve COMPLETE, not chain-by-chain.

## 3. Model statements

- **M1 — one field.**  Elevation is solved per CENTERLINE-GRAPH VERTEX
  over the full surviving apt.dat taxi network (plus discovered-rect
  axes), curves as polyline vertices with true arc lengths.  This field
  is the elevation truth for everything except runways (which keep
  their FAA profile machinery and provide the anchors).
- **M2 — intersections always agree.**  Wherever two centerlines cross
  or join, they share ONE graph vertex (split segments at
  intersections during graph build).  No ties, no consensus, no freeze:
  agreement by construction — exactly how crossing runways are handled.
- **M3 — runways anchor, and flex by demand.**  Hard anchors = runway
  contact vertices (centerline×runway-edge intersections, threshold
  zones) at the runway profile's values.  Where the network solve finds
  a contact infeasible against the rest of the field, it emits the
  runway-flex demand DIRECTLY (the demand = the profile value the
  network wants at the contact) — no transitive blocker-walking; the
  bounded re-smooth + one re-solve round stays (s73-p9/s77p2
  semantics, incl. DIP-only and the noise deadband).
- **M4 — the law on edges.**  Per-edge grade cap by role (taxi 1.5 %;
  apron-lane edges may carry the 1 % preference with 1.5 % law);
  Δg rate limit (`TAXIWAY_MAX_GRADE_CHANGE_PER_M`) through vertices
  along incident-edge pairs; curve-aware lengths everywhere (the s73
  ruling: grade applies along the centerline).
- **M5 — DEM-near objective; infeasibility SPREADS.**  Seed at DEM,
  project onto the constraint polytope (the runway-profile pattern at
  network scale).  Where hard anchors are jointly infeasible the
  residual is distributed minimax (every edge equally slightly
  over-cap) rather than concentrated.  ★ RULING TO CONFIRM with the
  user: an irreducible squeeze shows as ~1.6-1.9 % over a long stretch
  instead of a wall — user signalled yes ("G can only rise to about
  101... 23C would need to dip"), confirm once measured numbers exist.
- **M6 — geometry follows the field, never the reverse.**  Rect planes
  sample the field along their axis (the existing writeback shape);
  junction interiors twist between the crossing profiles (existing
  twist pass, line sources = graph edges); apron ring vertices bind via
  the s77 interior-geodesic value bands whose SEEDS become exact field
  samples along every lane (no more vertex-proximity seeding error);
  terminals leaf off apron medians (s77p2, unchanged).  The only
  feedback from geometry to the field is the runway flex (M3).

## 4. Data prerequisites (bounded, mechanical — do these first)

1. **Ingest the dropped centerlines as graph edges.**  HECA build log:
   "dropped 12 runway-crossing + 28 junction-buried centerline(s) (of
   172)".  They must exist IN THE GRAPH (they need no rects).  The
   runway-crossing lines become the runway-contact anchor sites; the
   junction-buried lines are the in-junction connectivity the twist
   needs.  Exit-fan curves (A4 class, s73-p10: apt.dat cuts the fan
   corner) need the curved exit line or the throat-fallback synthesis
   promoted into the graph.
2. **Split segments at every intersection** → shared vertices (M2).
   Curve polylines keep their intermediate vertices (arc lengths).
3. **Connectivity audit per airport**: components, which components
   touch a runway (anchor-less components re-anchor at DEM like
   today's chains), lane coverage of each apron (drives how much of M6
   binds).  A probe that renders per-vertex solved values vs DEM is the
   debugging backbone — build it first.
4. **Curve-aware length check** vs the ~4 % `ROUTE_NOISE_FRAC`: with
   true arc lengths the noise margin should SHRINK — re-measure, don't
   assume (the margin papers over under-measurement; the model wants it
   near zero).

## 5. Solver shape

`faa_joint_solve` generalized from a path to a graph:

- Variables: one elevation per graph vertex.  Hard: runway contacts.
- Constraints: |Δe| ≤ cap·len per edge; Δg rate through vertices per
  incident edge pair (the vertical-curve law along routes); optional
  apron-lane 1 % preference pass (best-effort, after the legal solve —
  the s77 zone-projection pattern).
- Objective: stay DEM-near (seed at DEM along the lines, iterate
  Gauss-Seidel projection with band clamps — the existing
  `_project_within_bands` shape over the graph; bands here are the
  anchor-feasibility intervals, computed by Dijkstra over the SAME
  graph, so they cannot disagree with the solve).
- Infeasibility: detect per-component (anchor pairs over-budget);
  distribute via uniform cap relaxation per component (minimax) — and
  emit runway-flex demands before relaxing (M3: pavement grades to max
  FIRST, then the runway flexes, then relax what remains).
- Determinism: iterate vertices/edges in sorted order; no
  hash-order-dependent state (PYTHONHASHSEED 1==2 byte-identity is a
  shipping gate).
- Scale: HECA ≈ 395 graph nodes / 332 edges + splits — trivial.  KPHX
  larger but still thousands; projection converges in milliseconds
  compared to the per-surface solves.

## 6. What it deletes / subsumes (measure before deleting — §5.6 doctrine)

- Stage A/B chain formation, crossing inserts, consensus, freeze,
  partial ties, blocker rescue, soft-terminus projection, post-freeze
  reconcile, bridge ties, apron-end pairs, the transitive demand walk —
  the entire tie layer (`_taxi_corridor_profiles` keeps only: mouth
  detection for WRITE mapping, the twist pass, the write layer).
- The st-band computation (stations read the field directly).
- The s77 geodesic-band SEEDING workaround (vertex-proximity +
  airside-visibility connector tests) — seeds become exact field
  samples at lane stations.
- Open items subsumed: #198/#208 connector + through-apron grading,
  Exit-3/exit-fan route holes, the #256 1.6 m residual, D #285, the
  #186/terminal7 squeeze placement (spreads), CYXY exit junctions.

## 7. Measured traps (s73–s77 lessons the implementation must respect)

- **Ship complete, not incremental** (s77p3: partial coupling 21→46
  walls).  The gate flips the whole network solve at once.
- **Writes need vertices**: the field lives on the graph; the surface
  has vertices only on rings/corners.  The mapping layer (M6) is where
  all previous write bugs lived — first-writer-wins twins, coupling
  coherence, skew end caps, pulled-vertex altitudes.  Keep those
  write-layer fixes; they apply unchanged to field sampling.
- **Terminals drag** (s76/s77 ×3): never let pads follow raw cap edges;
  median-of-adjacent-apron only (s77p2 leaf machinery — keep).
- **A5/singleton-virtual class** (p10c ruling): exit stubs whose top
  was anchored high were measured-rejected; under the network model the
  exit profile comes from the graph (with the curve/throat data of
  prereq 1) — re-verify A5 ≈ 60.1-60.4 flat and A4 explicitly.
- **One bounded flex round** (s68 over-dip vs s77p2 two-round
  convergence): demands measured on the network are hard-anchored and
  converge; keep round-2 grant + round-3 discard semantics until
  measured otherwise.
- **DEM smoothing gotcha**: probes with raw `tile_dem` differ from
  production; in-sim verdicts only from production-path builds.
- **Validator simultaneity** (the s64/s76 lesson): the validator's
  long-range law must read the SAME field (`route_field.py` extends to
  carry the solved profile); solver-only or validator-only shipping is
  a model violation.

## 8. Migration & gating

- Config gate `NETWORK_PROFILE_MODEL` (default OFF until the full
  validation battery passes; OFF = current dev behaviour
  byte-identical — the s77 all-gates-off identity test pattern).
- Steps, each measured: (1) graph build + ingest fixes + audit probe
  (no behaviour change); (2) network solve + field probe vs known
  invariants (still no writes); (3) flip writes: stations/twist/apron
  seeds read the field, tie layer bypassed; (4) validator reads the
  field; (5) measured deletes.
- Validation battery per step: per-axis audits (CYXY 0/0/0, SPJC 0/0/0,
  HECA within ≤ ~140 / 0 / 0 and trending DOWN), standalone >5 % walls
  ≤ 21 and trending down, invariants below, suite 307p/2f,
  determinism, gate-off byte-identity.

## 9. Invariants register (s77p3 ship state — verify each, in-sim where flagged)

- HECA: 05C/23C min 108.50 (user-predicted ~108; in-sim verdict
  pending), 05L/23R exactly 57.90–60.70, A4 ≈ 60.1–60.2, A5 flat
  60.9–61.1 (⚠ flagged; user ruling history says ~60.4 — the network
  model should land it from the graph, re-measure), thresholds 116.50,
  terminal1/2/9 = 99.80, terminal4 95.1, terminal5 64.3, #193 valley
  vertex ≥ ~102.4, #256 ring ≤ 1.6 m spread.
- CYXY: per-axis 0/0/0, apron spread sum ≤ 37.4.
- SPJC: per-axis 0/0/0 (watch R1/R2 — the bridge-tie deletion must be
  replaced by the shared crossing vertices before R1/R2 can pass).

## 10. Rulings register

Confirmed: grade applies along the centerline, curves carry it
(s73-p10); junction cross-axis diagonals unregulated; terminals =
natural leaf nodes, rigid-flat unless squeezed, follow aprons up or
down (s77p2); aprons prefer 1 %, stretch to 1.5 % where the route
demands (s77p2); taxiway A stays ~flat to the apron, A5 flat (p10c);
runways flex by measured demand, dip-or-rise symmetric (s68 revision),
DIP-only synthesis from corridor ties (s73-p9); "correct grade is king,
DEM is a starting point" (s73-p3).
To confirm: M5 spreading policy once first measured numbers exist
(predicted: the 05C↔A squeeze reads ~1.6-1.9 % over the G/T stretch
instead of any wall).
