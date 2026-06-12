# Apron-follows re-solve — terminals as a NATURAL RESULT of apron grading

★ USER QUESTIONS (2026-06-12) that define this design: *"Can we not
have the terminals elevation be a natural result of grading the apron
correctly in the initial solve?  Is the terminal currently pulling the
apron?"*  Answers: YES it can, and YES it is — this doc inverts the
dependency.

## 1. The finding: the terminal PULLS the apron (code-verified)

Mechanics today (unified_jacobi, terminal coherence ~L1797 + holds
~L1869):

1. Pads are re-leveled pre-apron-solve at
   ``min(median, band ceiling, taxi-route SEED ceiling)`` — the seed
   ceiling explicitly "pulls back" a pad the relief lifted (a remnant
   of the superseded "terminals must not rise" rule, still live in
   this path).
2. ``held_all = held_extra | _term_nodes`` then HOLDS every pad node
   through the apron projections — the pad is a fixed, terrain-low
   boundary condition, and the apron lawfully grades DOWN to it at
   ≤1.5 % → **the bowl is manufactured around the held pad**.
3. The LEAF re-level afterwards sets the pad from the median of the
   (bowled) adjacent apron — circular.  Every bound bolted onto this
   loop (s78p5 corridor-1 %-plane, s79 rect-seeds variant, the s79
   perpendicular-chord LIFT) fights symptom-side: the chord law lands
   the PAD correctly (HECA T1 100.1 → 102.7 ✓) but the apron stays
   bowled and the strain reappears as within-pairs (HECA #257: 33;
   SPJC #96: 2 — its GREEN gate, which is why TERMINAL_CHORD_LAW is
   gated OFF).

## 2. Target architecture: one-way dependency, no back-edges

Hierarchy (consistent with the network-profile-model philosophy — one
field drives, geometry follows):

```
network field (centerline graph, runway anchors)
   → taxi rects / junctions  (sample the field)
   → APRONS                  (corridor-plane TARGET in the zone,
                              DEM attraction beyond; pads exert NO
                              authority)
   → TERMINAL PADS           (inherit: median of their own settled
                              surface nodes, then flat)
```

Pads never influence anything upward.  The perpendicular-chord rule
(★ user ruling: every perpendicular chord from an intersecting taxi
centerline ≤ 1 %) holds **by construction** — the apron at the pad
face sits on the corridor plane, and the pad inherits it — so the
chord machinery demotes from a LIFT to a VALIDATOR.

### 2a. Pads become TRANSPARENT during the apron solve

Pad nodes participate as ORDINARY surface nodes: no hold, no rigid
flat-coupling during projections, no seed ceiling.  ⚠ This is NOT the
twice-measured-rejected "free pad" (s76 t7 70→72.9, s77 T1
99.7→106.1): those failures were a RIGID pad dragged wholesale by its
single worst neighbour (cap edges bind at the worst pair).  A
TRANSPARENT pad has no rigidity to drag — each node settles locally,
and flatness is imposed AFTER from the median (outlier-robust by
construction).

### 2b. The apron grades to the corridor plane (the actual fix)

The bowl's second parent is the relief's DEM attraction.  Inside the
corridor zone (the existing geodesic state: interior-path attribution,
two-rate 1 %/1.5 % law) the relief's attractor becomes the
CORRIDOR-PLANE VALUE (c_lo..c_hi midline) instead of the DEM; beyond
the zone, DEM attraction as today, with the two-rate transition the
machinery already computes.  This is an UPGRADE of the existing
best-effort clamp (`_apron_corridor_geodesic_state` consumers) — same
state, used as a target rather than only as bounds; NO new geometry
machinery.

### 2c. The rim absorbs the drop (existing machinery, one trigger edit)

Where the plane-held apron stands above terrain at its OUTER rim, the
s78p5 terrain-break edge retreat already renders the cliff (lower to
DEM + unweld + 10 m retreat + clearance face).  Its current guard
"interior-lane field median agrees with DEM median" exists to avoid
firing on intentionally-lifted interiors — under this model the
interior is INTENTIONALLY above DEM near pads, so the guard
generalizes: fire on rim runs that are (a) ≥2.5 m above local DEM,
(b) OUTSIDE the corridor zone, (c) not runway/route-anchored (the
existing F.band_lo floor guard stays).

## 3. Redundancy removal (the consolidation payoff)

| machinery | fate |
|---|---|
| taxi-route SEED CEILING on pads (`ceil_seed`, `_seed_terminals_from_taxi_routes` ceiling role) | DELETE — superseded by the s77p2 leaf ruling, still live in the coherence path; the bowl's first parent |
| pad holds through apron projections (`held_all` ∪ `_term_nodes`) | DELETE (transparency) |
| pad coherence re-level (min(median, ceiling, seed)) | becomes the post-solve INHERIT (median of own nodes → flat) |
| LEAF re-level (median of adjacent apron ≤40 m) | absorbed by INHERIT (the pad's own nodes ARE the settled surface) |
| chord-window LIFT + post-settle acceptance (s79, gated) | demote to VALIDATOR warn (`TERMINAL_CHORD_MAX_GRADE`); grade-grouping keeps the windows for pad-PAIR coherence (KPHL) |
| TERMINAL_PADS_SLOPE polish | KEEP for grade-grouping-infeasible complexes (pads slope on the now-correct apron) |
| s78p5 terrain-break retreat | KEEP, trigger generalized (§2c) |

Net: three pad-leveling mechanisms collapse into one inherit step; two
gated experiments retire; the corridor machinery gains one mode.

## 4. Predicted outcomes at the fixtures (the acceptance set)

* HECA T1/T2/T9 ≈ 102.3–102.7 (the chord validator confirms;
  serving stub B 102.3 / junction#291 103.0 at 36–40 m); #257's 33
  lift-strain pairs DON'T EXIST (the apron is already at the plane);
  terminal7 ≈ 70 (its serving lanes are at ~70 — the plane IS ~70).
* SPJC GREEN stays the hard gate: #96's pinned vert is reached by the
  plane-target relief instead of a post-hoc lift; if it is genuinely
  route-pinned, least-violation placement applies and the PAD inherits
  the settled (compromise) face — no 8 % pair, because the pad never
  out-runs its apron.  This property — *the pad cannot disagree with
  its own apron* — is the structural guarantee the lift approach
  lacked.
* KPHL terminal13/23 pair (grade-grouping fixture, f2d3053) must hold.
* Suite, CYXY 0/0/0, HECA invariants register, determinism,
  O4_PERF delta (one extra Dijkstra mode = ~nothing).

## 5. Work order

1. Gate `TERMINAL_NATURAL_LEVELS` (one gate for the whole inversion —
   partial application is the measured failure mode of this family).
   OFF = today's behaviour byte-identical.
2. Transparency + inherit (§2a, deletes from §3) — measure alone
   first: expect partial improvement (apron still DEM-attracted, but
   the pad anchor is gone, so the bowl shallows).
3. Corridor-plane relief target (§2b) — measure; T1 should land
   ~102.x with NO lift machinery involved.
4. Rim-retreat trigger generalization (§2c) — measure at HECA #198
   complex + T1 rim; SPLP/SPJC overlap gates (retreat history: the
   concave-ring/containment guards are measured-required).
5. Demote chord law to validator; delete the lift + acceptance code;
   flip `TERMINAL_CHORD_LAW` semantics to "validator warn" or fold
   into the standard checks.
6. Full battery + in-sim verdict (T1 slope from taxiways — the
   original complaint), then default ON and retire the gate.

## 6. Trap register

* The transparent-pad inherit must run BEFORE the final strict
  pair-law closure (the closure needs the pad's final flat value to
  arbitrate seams).
* Pad nodes shared with NON-apron shapes (junction-adjacent pads):
  transparency exposes them to junction writes — the inherit median
  is robust, but check the KPHL wedge-absorption interplay
  (f2d3053).
* `_term_nodes` is used for more than holds (band exemptions?) —
  audit every use before deleting holds.
* The corridor-plane target near LOW corridors (a terminal complex on
  genuinely sloping terrain — terminal7's ~70 vs the field): the
  plane follows the CORRIDOR's own profile, which slopes — the target
  is per-vertex c-mid, not one level.  t7 is the regression fixture.
* SPJC #96's lo>hi band verts: least-violation placement exists, but
  the plane target must not fight it per-sweep (oscillation) — target
  applies in the relief phase, bands clamp after, the established
  order.
